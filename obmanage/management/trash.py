"""Safe, preview-first cleanup for Obsidian's vault-level ``.trash`` folder.

The module deliberately has no Qt dependency.  Analysis is read-only.  New
cleanup operations directly and irreversibly remove only explicitly selected,
content-hashed preview entries after the whole selection has been revalidated.
The durable quarantine reader remains solely so installations upgraded from
2.0.0 can restore or finalize backups that already existed before this policy
changed; new cleanup operations never create backups or journals.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Iterable, Literal

from ..copying import copy_stream_and_hash
from ..models import SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, identity, native, snapshot


HASH_CHUNK_SIZE = 1024 * 1024
JOURNAL_VERSION = 1
AUTH_KEY_SIZE = 32
_WINDOWS_READONLY = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
_TRASH_TASK_LOCK = Lock()
_JOURNAL_STATUSES = {
    "backed_up", "success", "partial", "failed", "cancelled",
    "restored", "restore_partial", "finalizing", "finalize_failed",
    "finalize_partial", "finalized",
}


class TrashSafetyError(SyncError):
    """A trash operation could not continue without risking unrelated data."""


@dataclass(frozen=True)
class PathSnapshot:
    kind: str
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int


@dataclass(frozen=True)
class TrashEntry:
    relative_path: str
    kind: Literal["file", "dir"]
    size: int
    sha256: str
    snapshot: PathSnapshot


@dataclass(frozen=True)
class TrashVaultPreview:
    vault_root: str
    trash_path: str
    vault_snapshot: PathSnapshot
    obsidian_snapshot: PathSnapshot
    trash_snapshot: PathSnapshot
    entries: tuple[TrashEntry, ...]
    tree_sha256: str
    file_count: int
    dir_count: int
    total_bytes: int


@dataclass(frozen=True)
class TrashIssue:
    vault_root: str
    code: str
    message: str


@dataclass(frozen=True)
class TrashPlan:
    plan_id: str
    created_at: float
    vaults: tuple[TrashVaultPreview, ...]
    issues: tuple[TrashIssue, ...] = ()


@dataclass(frozen=True)
class TrashProgress:
    phase: str
    vault_root: str = ""
    relative_path: str = ""
    completed_bytes: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class TrashFailure:
    vault_root: str
    path: str
    phase: str
    message: str


@dataclass(frozen=True)
class TrashVaultResult:
    vault_root: str
    status: str
    removed_files: int = 0
    removed_dirs: int = 0
    removed_bytes: int = 0
    restored_files: int = 0
    restored_dirs: int = 0
    restored_bytes: int = 0
    message: str = ""


@dataclass(frozen=True)
class TrashOperationResult:
    status: str
    operation_id: str | None = None
    vaults: tuple[TrashVaultResult, ...] = ()
    failures: tuple[TrashFailure, ...] = ()
    bytes_freed: int = 0


@dataclass(frozen=True)
class TrashBackupRecord:
    vault_root: str
    backup_path: str
    status: str
    file_count: int
    dir_count: int
    total_bytes: int
    freed_bytes: int = 0


@dataclass(frozen=True)
class TrashOperation:
    operation_id: str
    created_at: float
    records: tuple[TrashBackupRecord, ...]


ProgressCallback = Callable[[TrashProgress], None] | None


class _StateFileLock:
    """One-byte advisory lock released by the OS when the process exits."""

    def __init__(self, path: str) -> None:
        self.path = canonical(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        assert_plain_chain(os.path.dirname(self.path))
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(native(self.path), flags, 0o600)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise TrashSafetyError("回收站任务锁不是普通文件。")
            path_state = _read_snapshot(self.path, "file")
            if (path_state.device, path_state.inode) != (opened.st_dev, opened.st_ino):
                raise TrashSafetyError("回收站任务锁在打开期间被替换。")
            if opened.st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            locked = os.fstat(fd)
            path_state = _read_snapshot(self.path, "file")
            if (path_state.device, path_state.inode) != (locked.st_dev, locked.st_ino):
                self._unlock_fd(fd)
                os.close(fd)
                raise TrashSafetyError("回收站任务锁在加锁期间被替换。")
            try:
                os.chmod(native(self.path), 0o600)
            except OSError:
                self._unlock_fd(fd)
                os.close(fd)
                raise
            self._fd = fd
            return True
        except BaseException:
            if self._fd is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        self._unlock_fd(fd)
        os.close(fd)


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("操作已取消。")


def _emit(callback: ProgressCallback, event: TrashProgress) -> None:
    if callback is not None:
        callback(event)


def _path_key(path: str | Path) -> str:
    value = canonical(path)
    value = canonical(os.path.realpath(native(value)))
    return os.path.normcase(value)


def _to_snapshot(value: dict | None, path: str, expected_kind: str | None = None) -> PathSnapshot:
    if value is None:
        raise TrashSafetyError(f"路径不存在：{path}")
    if expected_kind is not None and value["kind"] != expected_kind:
        if value["kind"] in ("link", "special"):
            raise TrashSafetyError(f"拒绝链接或特殊文件：{path}")
        raise TrashSafetyError(f"路径类型不正确：{path}")
    return PathSnapshot(
        kind=str(value["kind"]),
        size=int(value["size"]),
        mtime_ns=int(value["mtime_ns"]),
        ctime_ns=int(value["ctime_ns"]),
        inode=int(value["inode"]),
        device=int(value["device"]),
    )


def _read_snapshot(path: str, expected_kind: str | None = None) -> PathSnapshot:
    return _to_snapshot(snapshot(path), path, expected_kind)


def _same_identity(left: PathSnapshot, right: PathSnapshot) -> bool:
    return (left.kind, left.device, left.inode) == (right.kind, right.device, right.inode)


def _safe_child(root: str, relative_path: str) -> str:
    parts = relative_path.replace("\\", "/").split("/")
    if not relative_path or any(part in ("", ".", "..") for part in parts):
        raise TrashSafetyError(f"无效的回收站相对路径：{relative_path}")
    if os.name == "nt" and any(":" in part for part in parts):
        raise TrashSafetyError(f"无效的回收站相对路径：{relative_path}")
    candidate = canonical(os.path.join(root, *parts))
    try:
        common = os.path.commonpath((_path_key(root), _path_key(candidate)))
    except ValueError as exc:
        raise TrashSafetyError(f"回收站路径越界：{relative_path}") from exc
    if common != _path_key(root):
        raise TrashSafetyError(f"回收站路径越界：{relative_path}")
    assert_plain_chain(os.path.dirname(candidate))
    state = snapshot(candidate)
    if state is not None and state["kind"] in ("link", "special"):
        raise TrashSafetyError(f"拒绝链接或特殊文件：{candidate}")
    return candidate


def _validate_relative(relative_path: str) -> tuple[str, ...]:
    if not relative_path or "\\" in relative_path or os.path.isabs(relative_path):
        raise TrashSafetyError("隔离记录包含无效相对路径。")
    parts = tuple(relative_path.split("/"))
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise TrashSafetyError("隔离记录包含无效相对路径。")
    return parts


def _hash_file(path: str, expected: PathSnapshot, cancel: Event | None,
               progress: ProgressCallback, vault_root: str, relative_path: str) -> str:
    before = _read_snapshot(path, "file")
    if before != expected:
        raise TrashSafetyError(f"文件在读取前发生变化：{path}")
    digest = hashlib.sha256()
    completed = 0
    try:
        with open(native(path), "rb", buffering=0) as stream:
            while True:
                _check_cancel(cancel)
                block = stream.read(HASH_CHUNK_SIZE)
                if not block:
                    break
                digest.update(block)
                completed += len(block)
                _emit(progress, TrashProgress(
                    "hash", vault_root, relative_path, completed, expected.size
                ))
    except OSError as exc:
        raise TrashSafetyError(f"无法读取回收站文件：{path}（{exc}）") from exc
    after = _read_snapshot(path, "file")
    if after != expected or completed != expected.size:
        raise TrashSafetyError(f"文件在读取期间发生变化：{path}")
    return digest.hexdigest()


def _directory_digest(children: list[tuple[str, str, int, str]]) -> str:
    digest = hashlib.sha256()
    for name, kind, size, child_digest in sorted(children, key=lambda item: (item[0].casefold(), item[0])):
        payload = json.dumps(
            [name, kind, size, child_digest], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _scan_content(root: str, cancel: Event | None = None,
                  progress: ProgressCallback = None, vault_root: str = "") -> tuple[tuple[TrashEntry, ...], str]:
    root = canonical(root)
    assert_plain_chain(root)
    root_before = _read_snapshot(root, "dir")
    entries: list[TrashEntry] = []
    seen: set[str] = set()

    def walk(directory: str, relative_dir: str) -> tuple[str, int]:
        _check_cancel(cancel)
        directory_before = _read_snapshot(directory, "dir")
        try:
            with os.scandir(native(directory)) as iterator:
                names = sorted((entry.name for entry in iterator), key=lambda value: (value.casefold(), value))
        except OSError as exc:
            raise TrashSafetyError(f"无法扫描回收站目录：{directory}（{exc}）") from exc

        children: list[tuple[str, str, int, str]] = []
        total_bytes = 0
        for name in names:
            _check_cancel(cancel)
            relative = f"{relative_dir}/{name}" if relative_dir else name
            normalized = relative.replace("\\", "/").casefold()
            if normalized in seen:
                raise TrashSafetyError(f"存在仅大小写不同的冲突路径：{relative}")
            seen.add(normalized)
            child = _safe_child(root, relative)
            child_state = _read_snapshot(child)
            if child_state.kind == "file":
                child_digest = _hash_file(
                    child, child_state, cancel, progress, vault_root, relative
                )
                child_size = child_state.size
                entries.append(TrashEntry(relative, "file", child_size, child_digest, child_state))
            elif child_state.kind == "dir":
                assert_plain_chain(child)
                child_digest, child_size = walk(child, relative)
                child_after = _read_snapshot(child, "dir")
                if child_after != child_state:
                    raise TrashSafetyError(f"目录在扫描期间发生变化：{child}")
                entries.append(TrashEntry(relative, "dir", child_size, child_digest, child_state))
            else:
                raise TrashSafetyError(f"回收站内含链接或特殊文件，已拒绝处理：{child}")
            children.append((name, child_state.kind, child_size, child_digest))
            total_bytes += child_size

        if _read_snapshot(directory, "dir") != directory_before:
            raise TrashSafetyError(f"目录在扫描期间发生变化：{directory}")
        return _directory_digest(children), total_bytes

    tree_digest, _ = walk(root, "")
    if _read_snapshot(root, "dir") != root_before:
        raise TrashSafetyError(f"回收站在扫描期间发生变化：{root}")
    entries.sort(key=lambda item: (item.relative_path.casefold(), item.relative_path))
    return tuple(entries), tree_digest


def _validate_vault_root(value: str | Path, *, require_trash: bool = True) -> tuple[str, str, PathSnapshot, PathSnapshot, PathSnapshot | None]:
    text = os.fspath(value).strip()
    if not text:
        raise TrashSafetyError("仓库路径不能为空。")
    lexical = canonical(text)
    assert_plain_chain(lexical)
    root = canonical(os.path.realpath(native(lexical)))
    assert_plain_chain(root)
    root_state = _read_snapshot(root, "dir")

    obsidian = canonical(os.path.join(root, ".obsidian"))
    obsidian_state = _read_snapshot(obsidian, "dir")
    assert_plain_chain(obsidian)

    trash = canonical(os.path.join(root, ".trash"))
    trash_value = snapshot(trash)
    if trash_value is None:
        if require_trash:
            raise TrashSafetyError(f"仓库根目录下不存在 .trash：{root}")
        return root, trash, root_state, obsidian_state, None
    trash_state = _to_snapshot(trash_value, trash, "dir")
    assert_plain_chain(trash)
    return root, trash, root_state, obsidian_state, trash_state


def _scan_vault(value: str | Path, cancel: Event | None = None,
                progress: ProgressCallback = None) -> TrashVaultPreview:
    root, trash, root_before, obsidian_before, trash_before = _validate_vault_root(value)
    assert trash_before is not None
    entries, tree_digest = _scan_content(trash, cancel, progress, root)
    root_after = _read_snapshot(root, "dir")
    obsidian_after = _read_snapshot(canonical(os.path.join(root, ".obsidian")), "dir")
    trash_after = _read_snapshot(trash, "dir")
    if root_after != root_before or obsidian_after != obsidian_before or trash_after != trash_before:
        raise TrashSafetyError(f"仓库在分析期间发生变化：{root}")
    return TrashVaultPreview(
        vault_root=root,
        trash_path=trash,
        vault_snapshot=root_before,
        obsidian_snapshot=obsidian_before,
        trash_snapshot=trash_before,
        entries=entries,
        tree_sha256=tree_digest,
        file_count=sum(entry.kind == "file" for entry in entries),
        dir_count=sum(entry.kind == "dir" for entry in entries),
        total_bytes=sum(entry.size for entry in entries if entry.kind == "file"),
    )


def _preview_signature(preview: TrashVaultPreview) -> tuple:
    return (
        preview.vault_root,
        preview.trash_path,
        preview.vault_snapshot,
        preview.obsidian_snapshot,
        preview.trash_snapshot,
        preview.entries,
        preview.tree_sha256,
        preview.file_count,
        preview.dir_count,
        preview.total_bytes,
    )


def _recorded_root_identities_match(current: TrashVaultPreview,
                                    recorded: TrashVaultPreview) -> bool:
    return (
        current.vault_snapshot == recorded.vault_snapshot
        and current.obsidian_snapshot == recorded.obsidian_snapshot
        and _same_identity(current.trash_snapshot, recorded.trash_snapshot)
    )


def _matching_restored_counts(current: TrashVaultPreview,
                              expected: TrashVaultPreview) -> tuple[int, int, int]:
    current_by_path = {
        entry.relative_path: entry for entry in current.entries
    }
    files = dirs = total_bytes = 0
    for entry in expected.entries:
        observed = current_by_path.get(entry.relative_path)
        if observed is None:
            continue
        if (observed.kind, observed.size, observed.sha256) != (
            entry.kind, entry.size, entry.sha256
        ):
            continue
        if entry.kind == "file":
            files += 1
            total_bytes += entry.size
        else:
            dirs += 1
    return files, dirs, total_bytes


def _content_signature(entries: Iterable[TrashEntry]) -> tuple[tuple[str, str, int, str], ...]:
    return tuple(
        (entry.relative_path, entry.kind, entry.size, entry.sha256)
        for entry in sorted(entries, key=lambda item: (item.relative_path.casefold(), item.relative_path))
    )


def _plan_id(vaults: Iterable[TrashVaultPreview], issues: Iterable[TrashIssue]) -> str:
    body = {
        "vaults": [
            {
                "root": vault.vault_root,
                "tree": vault.tree_sha256,
                "trash": asdict(vault.trash_snapshot),
                "entries": list(_content_signature(vault.entries)),
            }
            for vault in vaults
        ],
        "issues": [asdict(issue) for issue in issues],
    }
    return hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _entry_to_json(entry: TrashEntry) -> dict:
    return {
        "relative_path": entry.relative_path,
        "kind": entry.kind,
        "size": entry.size,
        "sha256": entry.sha256,
        "snapshot": asdict(entry.snapshot),
    }


def _entry_from_json(value: dict) -> TrashEntry:
    if not isinstance(value, dict):
        raise TrashSafetyError("回收站记录包含无效项目。")
    relative = str(value["relative_path"])
    _validate_relative(relative)
    kind = str(value["kind"])
    if kind not in ("file", "dir"):
        raise TrashSafetyError("回收站记录包含无效项目类型。")
    digest = str(value["sha256"])
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise TrashSafetyError("回收站记录包含无效摘要。")
    snapshot_value = value["snapshot"]
    path_snapshot = PathSnapshot(
        kind=str(snapshot_value["kind"]),
        size=int(snapshot_value["size"]),
        mtime_ns=int(snapshot_value["mtime_ns"]),
        ctime_ns=int(snapshot_value["ctime_ns"]),
        inode=int(snapshot_value["inode"]),
        device=int(snapshot_value["device"]),
    )
    size = int(value["size"])
    if size < 0 or path_snapshot.kind != kind:
        raise TrashSafetyError("回收站记录中的项目快照无效。")
    if kind == "file" and (path_snapshot.size != size or size < 0):
        raise TrashSafetyError("回收站记录中的文件大小无效。")
    if kind == "dir" and path_snapshot.size != 0:
        raise TrashSafetyError("回收站记录中的目录快照无效。")
    return TrashEntry(relative, kind, size, digest, path_snapshot)  # type: ignore[arg-type]


def _preview_to_json(preview: TrashVaultPreview) -> dict:
    return {
        "vault_root": preview.vault_root,
        "trash_path": preview.trash_path,
        "vault_snapshot": asdict(preview.vault_snapshot),
        "obsidian_snapshot": asdict(preview.obsidian_snapshot),
        "trash_snapshot": asdict(preview.trash_snapshot),
        "entries": [_entry_to_json(entry) for entry in preview.entries],
        "tree_sha256": preview.tree_sha256,
        "file_count": preview.file_count,
        "dir_count": preview.dir_count,
        "total_bytes": preview.total_bytes,
    }


def _snapshot_from_json(value: dict) -> PathSnapshot:
    if not isinstance(value, dict):
        raise TrashSafetyError("隔离记录包含无效身份快照。")
    result = PathSnapshot(
        kind=str(value["kind"]), size=int(value["size"]),
        mtime_ns=int(value["mtime_ns"]), ctime_ns=int(value["ctime_ns"]),
        inode=int(value["inode"]), device=int(value["device"]),
    )
    if result.size < 0:
        raise TrashSafetyError("隔离记录包含无效身份快照。")
    return result


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _validate_preview_structure(preview: TrashVaultPreview) -> None:
    expected_trash = canonical(os.path.join(preview.vault_root, ".trash"))
    if preview.trash_path != expected_trash:
        raise TrashSafetyError("隔离记录中的 .trash 路径未绑定到仓库根目录。")
    if any(item.kind != "dir" for item in (
        preview.vault_snapshot, preview.obsidian_snapshot, preview.trash_snapshot
    )):
        raise TrashSafetyError("隔离记录中的仓库身份快照无效。")
    if not _is_sha256(preview.tree_sha256):
        raise TrashSafetyError("隔离记录中的目录摘要无效。")

    by_path: dict[str, TrashEntry] = {}
    by_casefold: set[str] = set()
    for entry in preview.entries:
        parts = _validate_relative(entry.relative_path)
        normalized = entry.relative_path.casefold()
        if normalized in by_casefold or entry.relative_path in by_path:
            raise TrashSafetyError("隔离记录包含重复或大小写冲突路径。")
        by_casefold.add(normalized)
        by_path[entry.relative_path] = entry
        if len(parts) > 1:
            parent = "/".join(parts[:-1])
            parent_entry = by_path.get(parent)
            # Entries are serialized in lexical order, in which a parent is
            # always encountered before its descendants.
            if parent_entry is None or parent_entry.kind != "dir":
                raise TrashSafetyError("隔离记录包含没有有效父目录的项目。")

    children: dict[str, list[tuple[str, str, int, str]]] = {"": []}
    for relative, entry in by_path.items():
        parent, _, name = relative.rpartition("/")
        children.setdefault(parent, []).append((name, entry.kind, entry.size, entry.sha256))
        if entry.kind == "dir":
            children.setdefault(relative, [])
    for relative in sorted(
        (path for path, entry in by_path.items() if entry.kind == "dir"),
        key=lambda path: -path.count("/"),
    ):
        entry = by_path[relative]
        expected_size = sum(item[2] for item in children[relative])
        expected_digest = _directory_digest(children[relative])
        if entry.size != expected_size or entry.sha256 != expected_digest:
            raise TrashSafetyError("隔离记录中的目录统计或摘要不一致。")

    root_digest = _directory_digest(children[""])
    file_count = sum(entry.kind == "file" for entry in preview.entries)
    dir_count = sum(entry.kind == "dir" for entry in preview.entries)
    total_bytes = sum(entry.size for entry in preview.entries if entry.kind == "file")
    if (root_digest != preview.tree_sha256 or file_count != preview.file_count
            or dir_count != preview.dir_count or total_bytes != preview.total_bytes):
        raise TrashSafetyError("隔离记录中的汇总统计或摘要不一致。")


def _preview_from_json(value: dict) -> TrashVaultPreview:
    if not isinstance(value, dict):
        raise TrashSafetyError("隔离记录包含无效仓库预览。")
    entries = tuple(_entry_from_json(item) for item in value["entries"])
    preview = TrashVaultPreview(
        vault_root=canonical(str(value["vault_root"])),
        trash_path=canonical(str(value["trash_path"])),
        vault_snapshot=_snapshot_from_json(value["vault_snapshot"]),
        obsidian_snapshot=_snapshot_from_json(value["obsidian_snapshot"]),
        trash_snapshot=_snapshot_from_json(value["trash_snapshot"]),
        entries=entries,
        tree_sha256=str(value["tree_sha256"]),
        file_count=int(value["file_count"]),
        dir_count=int(value["dir_count"]),
        total_bytes=int(value["total_bytes"]),
    )
    _validate_preview_structure(preview)
    return preview


def _copy_file_verified(source: str, destination: str, expected: TrashEntry,
                        cancel: Event | None, progress: ProgressCallback,
                        vault_root: str, phase: str) -> None:
    before = _read_snapshot(source, "file")
    if before != expected.snapshot:
        raise TrashSafetyError(f"复制前文件已改变：{source}")
    try:
        with open(native(source), "rb", buffering=0) as src, open(native(destination), "xb", buffering=0) as dst:
            def report(completed_bytes: int) -> None:
                _emit(progress, TrashProgress(
                    phase, vault_root, expected.relative_path, completed_bytes, expected.size
                ))

            completed, digest = copy_stream_and_hash(
                src,
                dst,
                expected.snapshot.size,
                check_cancel=lambda: _check_cancel(cancel),
                progress=report,
            )
        if completed != expected.snapshot.size or digest != expected.sha256:
            raise TrashSafetyError(f"复制内容校验失败：{source}")
        if _read_snapshot(source, "file") != expected.snapshot:
            raise TrashSafetyError(f"复制期间文件已改变：{source}")
        shutil.copystat(native(source), native(destination), follow_symlinks=False)
    except BaseException:
        try:
            if snapshot(destination) is not None:
                os.chmod(native(destination), stat.S_IWRITE)
                os.unlink(native(destination))
        except OSError:
            pass
        raise


def _copy_entries(source_root: str, destination_root: str, entries: tuple[TrashEntry, ...],
                  cancel: Event | None, progress: ProgressCallback,
                  vault_root: str, phase: str, *, destination_exists: bool) -> None:
    if destination_exists:
        if _read_snapshot(destination_root, "dir").kind != "dir":
            raise TrashSafetyError(f"恢复目标不是目录：{destination_root}")
        try:
            with os.scandir(native(destination_root)) as iterator:
                if next(iterator, None) is not None:
                    raise TrashSafetyError(f"恢复目标已有内容，拒绝覆盖：{destination_root}")
        except OSError as exc:
            raise TrashSafetyError(f"无法检查恢复目标：{destination_root}（{exc}）") from exc
    else:
        os.mkdir(native(destination_root))

    directories = sorted(
        (entry for entry in entries if entry.kind == "dir"),
        key=lambda item: (item.relative_path.count("/"), item.relative_path.casefold(), item.relative_path),
    )
    files = sorted(
        (entry for entry in entries if entry.kind == "file"),
        key=lambda item: (item.relative_path.casefold(), item.relative_path),
    )
    try:
        for entry in directories:
            _check_cancel(cancel)
            source = _safe_child(source_root, entry.relative_path)
            if _read_snapshot(source, "dir") != entry.snapshot:
                raise TrashSafetyError(f"复制前目录已改变：{source}")
            os.mkdir(native(_safe_child(destination_root, entry.relative_path)))
        for entry in files:
            _check_cancel(cancel)
            _copy_file_verified(
                _safe_child(source_root, entry.relative_path),
                _safe_child(destination_root, entry.relative_path),
                entry, cancel, progress, vault_root, phase,
            )
        for entry in reversed(directories):
            source = _safe_child(source_root, entry.relative_path)
            if not _same_identity(_read_snapshot(source, "dir"), entry.snapshot):
                raise TrashSafetyError(f"复制期间目录已被替换：{source}")
            shutil.copystat(
                native(source), native(_safe_child(destination_root, entry.relative_path)),
                follow_symlinks=False,
            )
    except BaseException:
        raise


def _unlink_file(path: str, expected: PathSnapshot) -> None:
    current = _read_snapshot(path, "file")
    if current != expected:
        raise TrashSafetyError(f"删除前文件已改变：{path}")
    access_error: PermissionError | None = None
    try:
        os.unlink(native(path))
        return
    except PermissionError as exc:
        if os.name != "nt":
            raise
        access_error = exc

    verified = _read_snapshot(path, "file")
    if verified != expected:
        raise TrashSafetyError(f"文件在权限处理前已改变：{path}")
    old_stat = os.lstat(native(path))
    if not getattr(old_stat, "st_file_attributes", 0) & _WINDOWS_READONLY:
        assert access_error is not None
        raise access_error
    old_mode = old_stat.st_mode
    os.chmod(native(path), old_mode | stat.S_IWRITE)
    writable = _read_snapshot(path, "file")
    if not _same_identity(writable, expected) or writable.size != expected.size or writable.mtime_ns != expected.mtime_ns:
        raise TrashSafetyError(f"文件在权限处理时被替换，已拒绝删除：{path}")
    try:
        os.unlink(native(path))
    except BaseException:
        try:
            if _same_identity(_read_snapshot(path, "file"), writable):
                os.chmod(native(path), old_mode)
        except (OSError, TrashSafetyError):
            pass
        raise


def _clear_preview(preview: TrashVaultPreview, cancel: Event | None,
                   progress: ProgressCallback) -> tuple[TrashVaultResult, tuple[TrashFailure, ...]]:
    failures: list[TrashFailure] = []
    removed_files = 0
    removed_dirs = 0
    removed_bytes = 0
    files = sorted(
        (entry for entry in preview.entries if entry.kind == "file"),
        key=lambda item: (-item.relative_path.count("/"), item.relative_path.casefold(), item.relative_path),
    )
    directories = sorted(
        (entry for entry in preview.entries if entry.kind == "dir"),
        key=lambda item: (-item.relative_path.count("/"), item.relative_path.casefold(), item.relative_path),
    )
    try:
        for entry in files:
            _check_cancel(cancel)
            if not _same_identity(_read_snapshot(preview.trash_path, "dir"), preview.trash_snapshot):
                raise TrashSafetyError(f"回收站目录已被替换：{preview.trash_path}")
            path = _safe_child(preview.trash_path, entry.relative_path)
            try:
                _unlink_file(path, entry.snapshot)
                removed_files += 1
                removed_bytes += entry.size
                _emit(progress, TrashProgress(
                    "clear", preview.vault_root, entry.relative_path,
                    removed_bytes, preview.total_bytes,
                ))
            except (OSError, TrashSafetyError) as exc:
                failures.append(TrashFailure(
                    preview.vault_root, path, "clear", str(exc)
                ))
        for entry in directories:
            _check_cancel(cancel)
            path = _safe_child(preview.trash_path, entry.relative_path)
            try:
                current = _read_snapshot(path, "dir")
                if not _same_identity(current, entry.snapshot):
                    raise TrashSafetyError(f"删除前目录已被替换：{path}")
                os.rmdir(native(path))
                removed_dirs += 1
            except (OSError, TrashSafetyError) as exc:
                failures.append(TrashFailure(
                    preview.vault_root, path, "clear", str(exc)
                ))
    except SyncCancelled:
        status = "cancelled"
        message = "清理已取消；已删除的项目无法恢复。"
    except (OSError, TrashSafetyError) as exc:
        failures.append(TrashFailure(
            preview.vault_root, preview.trash_path, "clear", str(exc)
        ))
        status = "partial"
        message = "清理因路径或身份复核失败而中止；已删除的项目无法恢复。"
    else:
        status = "success" if not failures else "partial"
        message = "回收站已清空并保留目录。" if not failures else "部分项目未能移除；已删除的项目无法恢复。"

    try:
        remaining = tuple(os.scandir(native(preview.trash_path)))
    except OSError as exc:
        failures.append(TrashFailure(preview.vault_root, preview.trash_path, "verify", str(exc)))
        if status == "success":
            status = "partial"
            message = "无法验证清理结果；已删除的项目无法恢复。"
    else:
        if remaining and status == "success":
            status = "partial"
            message = "回收站仍有内容；未预览项目未被删除。"
            failures.append(TrashFailure(
                preview.vault_root, preview.trash_path, "verify",
                "回收站在执行期间出现未预览内容。",
            ))
    return TrashVaultResult(
        preview.vault_root, status, removed_files, removed_dirs,
        removed_bytes, message=message,
    ), tuple(failures)


def _remove_backup_tree(root: str, cancel: Event | None = None, *,
                        expected_root: PathSnapshot | None = None,
                        expected_entries: tuple[TrashEntry, ...]) -> tuple[int, int, int, list[TrashFailure], bool]:
    failures: list[TrashFailure] = []
    current_root = snapshot(root)
    if current_root is None:
        return 0, 0, 0, failures, False
    root_snapshot = _to_snapshot(current_root, root, "dir")
    if expected_root is not None and not _same_identity(root_snapshot, expected_root):
        raise TrashSafetyError(f"隔离目录已被替换，拒绝清理：{root}")
    entries, _ = _scan_content(root, cancel)
    # The preflight scan and the destructive pass are intentionally separate:
    # another same-user process can still add content between them.  Never use
    # that later mutable directory listing as deletion authority.  Rebind every
    # candidate to the authenticated/original manifest before removing the
    # first item; an unknown or content-changed item leaves the whole tree
    # untouched for a later, explicit retry.
    _validated_manifest_subset(entries, expected_entries, require_strict=False)
    files = sorted(
        (entry for entry in entries if entry.kind == "file"),
        key=lambda item: (-item.relative_path.count("/"), item.relative_path.casefold(), item.relative_path),
    )
    directories = sorted(
        (entry for entry in entries if entry.kind == "dir"),
        key=lambda item: (-item.relative_path.count("/"), item.relative_path.casefold(), item.relative_path),
    )
    removed_files = removed_dirs = removed_bytes = 0
    for entry in files:
        if cancel is not None and cancel.is_set():
            return removed_files, removed_dirs, removed_bytes, failures, True
        path = _safe_child(root, entry.relative_path)
        try:
            if not _same_identity(_read_snapshot(root, "dir"), root_snapshot):
                raise TrashSafetyError(f"隔离目录在清理期间被替换：{root}")
            _unlink_file(path, entry.snapshot)
            removed_files += 1
            removed_bytes += entry.size
        except (OSError, TrashSafetyError) as exc:
            failures.append(TrashFailure("", path, "finalize", str(exc)))
    for entry in directories:
        if cancel is not None and cancel.is_set():
            return removed_files, removed_dirs, removed_bytes, failures, True
        path = _safe_child(root, entry.relative_path)
        try:
            if not _same_identity(_read_snapshot(root, "dir"), root_snapshot):
                raise TrashSafetyError(f"隔离目录在清理期间被替换：{root}")
            if not _same_identity(_read_snapshot(path, "dir"), entry.snapshot):
                raise TrashSafetyError(f"隔离子目录在清理期间被替换：{path}")
            os.rmdir(native(path))
            removed_dirs += 1
        except (OSError, TrashSafetyError) as exc:
            failures.append(TrashFailure("", path, "finalize", str(exc)))
    if cancel is not None and cancel.is_set():
        return removed_files, removed_dirs, removed_bytes, failures, True
    try:
        if not _same_identity(_read_snapshot(root, "dir"), root_snapshot):
            raise TrashSafetyError(f"隔离目录在最终清理前被替换：{root}")
        os.rmdir(native(root))
        removed_dirs += 1
    except (OSError, TrashSafetyError) as exc:
        failures.append(TrashFailure("", root, "finalize", str(exc)))
    return removed_files, removed_dirs, removed_bytes, failures, False


def _validated_manifest_subset(current: tuple[TrashEntry, ...],
                               original: tuple[TrashEntry, ...], *,
                               require_strict: bool) -> int:
    """Validate a remaining backup without trusting mutable directory digests.

    Files must retain their original content digest and size.  Directories must
    retain their original path and kind; their digest is allowed to represent
    the already-removed subset of children.  ``_scan_content`` has already
    proven the current parent structure and all current directory digests.
    """
    original_by_path = {entry.relative_path: entry for entry in original}
    if require_strict and len(current) >= len(original):
        raise TrashSafetyError("部分清理记录没有形成原清单的严格子集。")
    remaining_bytes = 0
    for entry in current:
        expected = original_by_path.get(entry.relative_path)
        if expected is None or expected.kind != entry.kind:
            raise TrashSafetyError("隔离目录出现原认证清单之外的项目。")
        if entry.kind == "file":
            if entry.size != expected.size or entry.sha256 != expected.sha256:
                raise TrashSafetyError("隔离目录中的剩余文件与原认证摘要不一致。")
            remaining_bytes += entry.size
    return remaining_bytes


class TrashCleanupEngine:
    """Analyze and directly clear selected vault trash folders.

    ``list_operations``, ``restore`` and ``finalize`` are legacy migration
    support for quarantine journals created by ObManage 2.0.0.  ``execute``
    intentionally does not call any backup or journal helper.
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = canonical(state_dir)
        self._backup_base = canonical(os.path.join(self.state_dir, "trash_backups"))
        self._journal_base = canonical(os.path.join(self.state_dir, "trash_journal"))
        self._lock_path = canonical(os.path.join(self.state_dir, "trash-operation.lock"))
        self._auth_key_path = canonical(os.path.join(self.state_dir, "trash-auth.key"))

    def _acquire_file_lock(self) -> _StateFileLock | None:
        os.makedirs(native(self.state_dir), mode=0o700, exist_ok=True)
        assert_plain_chain(self.state_dir)
        _read_snapshot(self.state_dir, "dir")
        lock = _StateFileLock(self._lock_path)
        return lock if lock.acquire() else None

    def analyze(self, vault_roots: Iterable[str | Path], *, cancel: Event | None = None,
                progress: ProgressCallback = None) -> TrashPlan:
        if not _TRASH_TASK_LOCK.acquire(blocking=False):
            raise TrashSafetyError("另一个仓库管理任务正在运行，请稍后重试。")
        try:
            return self._analyze_locked(vault_roots, cancel=cancel, progress=progress)
        finally:
            _TRASH_TASK_LOCK.release()

    def _analyze_locked(self, vault_roots: Iterable[str | Path], *,
                        cancel: Event | None = None,
                        progress: ProgressCallback = None) -> TrashPlan:
        vaults: list[TrashVaultPreview] = []
        issues: list[TrashIssue] = []
        seen: set[str] = set()
        for value in vault_roots:
            _check_cancel(cancel)
            display = canonical(value)
            key = _path_key(display)
            if key in seen:
                issues.append(TrashIssue(display, "duplicate", "重复的仓库根目录已跳过。"))
                continue
            seen.add(key)
            try:
                preview = _scan_vault(value, cancel, progress)
            except (OSError, SyncError) as exc:
                message = str(exc)
                code = "missing_trash" if "不存在 .trash" in message else "unsafe"
                issues.append(TrashIssue(display, code, message))
            else:
                vaults.append(preview)
        vaults.sort(key=lambda item: (_path_key(item.vault_root), item.vault_root))
        issues.sort(key=lambda item: (_path_key(item.vault_root), item.code))
        return TrashPlan(
            _plan_id(vaults, issues), time.time(), tuple(vaults), tuple(issues)
        )

    def _validate_state_location(self, previews: Iterable[TrashVaultPreview]) -> None:
        state_key = _path_key(self.state_dir)
        for preview in previews:
            root_key = _path_key(preview.vault_root)
            try:
                common = os.path.commonpath((state_key, root_key))
            except ValueError:
                continue
            if common in (state_key, root_key):
                raise TrashSafetyError("应用状态目录不能位于仓库内，仓库也不能位于状态目录内。")
        assert_plain_chain(self.state_dir)

    @staticmethod
    def _selected(plan: TrashPlan, selected_vaults: Iterable[str | Path]) -> tuple[TrashVaultPreview, ...]:
        values = list(selected_vaults)
        if not values:
            raise TrashSafetyError("请明确选择至少一个已预览的回收站。")
        available = {_path_key(item.vault_root): item for item in plan.vaults}
        selected: list[TrashVaultPreview] = []
        seen: set[str] = set()
        for value in values:
            key = _path_key(value)
            if key in seen:
                continue
            seen.add(key)
            if key not in available:
                raise TrashSafetyError(f"所选仓库不在本次预览中：{canonical(value)}")
            selected.append(available[key])
        return tuple(selected)

    @staticmethod
    def _revalidate_all(previews: Iterable[TrashVaultPreview], cancel: Event | None,
                        progress: ProgressCallback) -> None:
        for expected in previews:
            current = _scan_vault(expected.vault_root, cancel, progress)
            if _preview_signature(current) != _preview_signature(expected):
                raise TrashSafetyError(f"回收站内容已变化，请重新分析：{expected.vault_root}")

    def _ensure_state_dirs(self) -> None:
        os.makedirs(native(self._backup_base), exist_ok=True)
        os.makedirs(native(self._journal_base), exist_ok=True)
        assert_plain_chain(self._backup_base)
        assert_plain_chain(self._journal_base)

    def _has_persistent_operation_state(self) -> bool:
        """Return whether a missing key would orphan existing authority.

        Any entry is evidence, including a corrupt journal or an uncommitted
        backup left by a crash.  In that situation generating a new key would
        make the old records unverifiable while authorizing unrelated new
        operations, so callers must fail closed.
        """
        for directory in (self._journal_base, self._backup_base):
            value = snapshot(directory)
            if value is None:
                continue
            _read_snapshot(directory, "dir")
            assert_plain_chain(directory)
            try:
                with os.scandir(native(directory)) as iterator:
                    if next(iterator, None) is not None:
                        return True
            except OSError as exc:
                raise TrashSafetyError(f"无法检查既有隔离状态：{directory}（{exc}）") from exc
        return False

    def _publish_auth_key(self) -> None:
        """Write a complete key before atomically publishing its final name."""
        key = secrets.token_bytes(AUTH_KEY_SIZE)
        temp = canonical(self._auth_key_path + f".tmp-{uuid.uuid4()}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(native(temp), flags, 0o600)
            view = memoryview(key)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("无法写入隔离认证密钥。")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.chmod(native(temp), 0o600)
            if snapshot(self._auth_key_path) is not None:
                return
            # All legitimate writers hold the state-directory operation lock.
            # The replace is the publication point: the final path is either
            # absent or contains a fully written, fsynced key.
            os.replace(native(temp), native(self._auth_key_path))
            temp = ""
            os.chmod(native(self._auth_key_path), 0o600)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temp:
                try:
                    os.unlink(native(temp))
                except FileNotFoundError:
                    pass

    def _auth_key(self, *, create: bool) -> bytes:
        state = snapshot(self._auth_key_path)
        if state is None:
            if not create:
                raise TrashSafetyError("隔离认证密钥缺失，已拒绝使用持久记录。")
            if self._has_persistent_operation_state():
                raise TrashSafetyError(
                    "隔离认证密钥缺失但已有持久记录或备份，已拒绝创建新密钥。"
                )
            self._publish_auth_key()
            state = snapshot(self._auth_key_path)
            if state is None:
                # A conforming writer can only reach this path if another
                # publisher won a race and then removed its key.  Never retry
                # blindly because authority may have existed in between.
                raise TrashSafetyError("隔离认证密钥未能安全发布。")
        key_state = _to_snapshot(state, self._auth_key_path, "file")
        if key_state.size != AUTH_KEY_SIZE:
            if not create or self._has_persistent_operation_state():
                raise TrashSafetyError("隔离认证密钥无效，已拒绝使用持久记录。")
            # Older versions published the final path before writing all key
            # bytes.  It is safe to repair only when no journal or backup can
            # possibly depend on that incomplete value.
            _unlink_file(self._auth_key_path, key_state)
            if snapshot(self._auth_key_path) is not None:
                raise TrashSafetyError("无法安全移除残缺的隔离认证密钥。")
            self._publish_auth_key()
        before = _read_snapshot(self._auth_key_path, "file")
        try:
            with open(native(self._auth_key_path), "rb", buffering=0) as stream:
                key = stream.read(AUTH_KEY_SIZE + 1)
        except OSError as exc:
            raise TrashSafetyError(f"无法读取隔离认证密钥：{exc}") from exc
        after = _read_snapshot(self._auth_key_path, "file")
        if (not _same_identity(before, after) or before.size != after.size
                or len(key) != AUTH_KEY_SIZE):
            raise TrashSafetyError("隔离认证密钥无效或读取期间发生变化。")
        return key

    @staticmethod
    def _authenticated_payload(data: dict) -> bytes:
        unsigned = {key: value for key, value in data.items() if key != "hmac_sha256"}
        return json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _sign_journal(self, data: dict) -> dict:
        signed = {key: value for key, value in data.items() if key != "hmac_sha256"}
        signed["hmac_sha256"] = hmac.new(
            self._auth_key(create=True), self._authenticated_payload(signed), hashlib.sha256
        ).hexdigest()
        return signed

    def _verify_journal_authentication(self, data: dict) -> None:
        signature = data.get("hmac_sha256")
        if not isinstance(signature, str) or not _is_sha256(signature):
            raise TrashSafetyError("隔离记录缺少有效认证签名。")
        expected = hmac.new(
            self._auth_key(create=False), self._authenticated_payload(data), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise TrashSafetyError("隔离记录认证失败，可能已损坏或被篡改。")

    @staticmethod
    def _valid_operation_id(operation_id: str) -> str:
        try:
            parsed = uuid.UUID(operation_id)
        except (ValueError, AttributeError) as exc:
            raise TrashSafetyError("无效的隔离操作编号。") from exc
        value = str(parsed)
        if value != operation_id:
            raise TrashSafetyError("无效的隔离操作编号。")
        return value

    def _journal_path(self, operation_id: str) -> str:
        value = self._valid_operation_id(operation_id)
        return canonical(os.path.join(self._journal_base, f"{value}.json"))

    def _operation_backup_root(self, operation_id: str) -> str:
        value = self._valid_operation_id(operation_id)
        return canonical(os.path.join(self._backup_base, value))

    def _save_journal(self, data: dict) -> None:
        self._ensure_state_dirs()
        path = self._journal_path(str(data["operation_id"]))
        temp = canonical(path + f".tmp-{uuid.uuid4()}")
        signed = self._sign_journal(data)
        try:
            with open(native(temp), "x", encoding="utf-8", newline="\n") as stream:
                json.dump(signed, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(native(temp), native(path))
            data.clear()
            data.update(signed)
        finally:
            try:
                os.unlink(native(temp))
            except FileNotFoundError:
                pass

    def _load_journal(self, operation_id: str) -> dict:
        path = self._journal_path(operation_id)
        try:
            with open(native(path), "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise TrashSafetyError(f"无法读取隔离记录：{operation_id}（{exc}）") from exc
        if not isinstance(data, dict):
            raise TrashSafetyError("隔离记录格式无效。")
        if data.get("version") != JOURNAL_VERSION or data.get("operation_id") != operation_id:
            raise TrashSafetyError("隔离记录格式无效。")
        self._verify_journal_authentication(data)
        created_at = data.get("created_at")
        if (not isinstance(created_at, (int, float)) or isinstance(created_at, bool)
                or not math.isfinite(created_at) or created_at < 0):
            raise TrashSafetyError("隔离记录包含无效时间。")
        vault_values = data.get("vaults")
        if not isinstance(vault_values, list) or not vault_values:
            raise TrashSafetyError("隔离记录缺少仓库列表。")
        backup_root_snapshot = _snapshot_from_json(data.get("backup_root_snapshot"))
        if backup_root_snapshot.kind != "dir":
            raise TrashSafetyError("隔离记录中的操作根身份无效。")
        seen_roots: set[str] = set()
        for index, value in enumerate(vault_values):
            if not isinstance(value, dict):
                raise TrashSafetyError("隔离记录包含无效仓库项。")
            if value.get("backup_rel") != f"vault_{index:04d}":
                raise TrashSafetyError("隔离记录中的备份目录与操作编号不匹配。")
            if value.get("status") not in _JOURNAL_STATUSES:
                raise TrashSafetyError("隔离记录包含无效状态。")
            preview = _preview_from_json(value.get("preview"))
            backup_snapshot = _snapshot_from_json(value.get("backup_snapshot"))
            if backup_snapshot.kind != "dir":
                raise TrashSafetyError("隔离记录中的备份身份无效。")
            freed_bytes = value.get("freed_bytes")
            if (not isinstance(freed_bytes, int) or isinstance(freed_bytes, bool)
                    or not 0 <= freed_bytes <= preview.total_bytes):
                raise TrashSafetyError("隔离记录中的释放空间统计无效。")
            if value.get("status") == "finalized" and freed_bytes != preview.total_bytes:
                raise TrashSafetyError("已最终清理记录的释放空间统计不完整。")
            key = _path_key(preview.vault_root)
            if key in seen_roots:
                raise TrashSafetyError("隔离记录包含重复仓库。")
            seen_roots.add(key)
            self._backup_path(operation_id, str(value["backup_rel"]))
        return data

    def _backup_path(self, operation_id: str, relative: str) -> str:
        if (not relative.startswith("vault_") or len(relative) != 10
                or not relative[6:].isdigit()
                or "/" in relative or "\\" in relative or relative in (".", "..")):
            raise TrashSafetyError("隔离记录包含无效备份路径。")
        root = self._operation_backup_root(operation_id)
        path = canonical(os.path.join(root, relative))
        if os.path.commonpath((_path_key(root), _path_key(path))) != _path_key(root):
            raise TrashSafetyError("隔离记录中的备份路径越界。")
        return path

    def _verify_operation_root(self, data: dict, *, allow_finalized_missing: bool = False) -> tuple[str, PathSnapshot] | None:
        operation_id = str(data["operation_id"])
        path = self._operation_backup_root(operation_id)
        expected = _snapshot_from_json(data["backup_root_snapshot"])
        current_value = snapshot(path)
        if current_value is None:
            if allow_finalized_missing and all(
                value.get("status") == "finalized" for value in data["vaults"]
            ):
                return None
            raise TrashSafetyError("隔离操作根目录缺失，已拒绝继续。")
        current = _to_snapshot(current_value, path, "dir")
        if not _same_identity(current, expected):
            raise TrashSafetyError("隔离操作根目录已被替换，已拒绝继续。")
        return path, expected

    def _verify_backup_root(self, operation_id: str, value: dict) -> tuple[str, PathSnapshot]:
        path = self._backup_path(operation_id, str(value["backup_rel"]))
        expected = _snapshot_from_json(value["backup_snapshot"])
        current = _read_snapshot(path, "dir")
        if not _same_identity(current, expected):
            raise TrashSafetyError("隔离备份目录已被替换，已拒绝继续。")
        return path, expected

    @staticmethod
    def _cleanup_uncommitted_operation(
        operation_root: str,
        operation_anchor: PathSnapshot | None,
        owned_backups: list[tuple[str, PathSnapshot, tuple[TrashEntry, ...]]],
    ) -> None:
        for backup_path, backup_anchor, expected_entries in reversed(owned_backups):
            try:
                _remove_backup_tree(
                    backup_path,
                    expected_root=backup_anchor,
                    expected_entries=expected_entries,
                )
            except (OSError, SyncError):
                pass
        if operation_anchor is None:
            return
        try:
            current = _read_snapshot(operation_root, "dir")
            if not _same_identity(current, operation_anchor):
                return
            with os.scandir(native(operation_root)) as iterator:
                if next(iterator, None) is not None:
                    return
            os.rmdir(native(operation_root))
        except (OSError, SyncError):
            pass

    def _remove_finalized_operation_root(self, data: dict) -> None:
        if not all(value.get("status") == "finalized" for value in data["vaults"]):
            return
        verified = self._verify_operation_root(data, allow_finalized_missing=True)
        if verified is None:
            return
        path, expected = verified
        current = _read_snapshot(path, "dir")
        if not _same_identity(current, expected):
            raise TrashSafetyError("隔离操作根目录已被替换，拒绝移除。")
        with os.scandir(native(path)) as iterator:
            if next(iterator, None) is not None:
                raise TrashSafetyError("全部备份已清理，但操作根目录仍含未知项目。")
        os.rmdir(native(path))

    def _operation_from_data(self, data: dict) -> TrashOperation:
        operation_id = str(data["operation_id"])
        records: list[TrashBackupRecord] = []
        for value in data["vaults"]:
            preview = _preview_from_json(value["preview"])
            records.append(TrashBackupRecord(
                preview.vault_root,
                self._backup_path(operation_id, str(value["backup_rel"])),
                str(value["status"]),
                preview.file_count,
                preview.dir_count,
                preview.total_bytes,
                int(value["freed_bytes"]),
            ))
        return TrashOperation(operation_id, float(data["created_at"]), tuple(records))

    def get_operation(self, operation_id: str) -> TrashOperation:
        data = self._load_journal(operation_id)
        return self._operation_from_data(data)

    def _legacy_backup_operation_ids(self) -> set[str]:
        """Return authenticated-namespace backup roots without adopting them."""
        if snapshot(self._backup_base) is None:
            return set()
        assert_plain_chain(self._backup_base)
        _read_snapshot(self._backup_base, "dir")
        try:
            with os.scandir(native(self._backup_base)) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as exc:
            raise TrashSafetyError(f"无法枚举旧版隔离备份：{exc}") from exc

        operation_ids: set[str] = set()
        for name in names:
            try:
                operation_id = self._valid_operation_id(name)
            except TrashSafetyError as exc:
                raise TrashSafetyError(
                    f"旧版隔离备份目录包含未知项目：{name}"
                ) from exc
            _read_snapshot(self._operation_backup_root(operation_id), "dir")
            operation_ids.add(operation_id)
        return operation_ids

    def list_operations(self) -> tuple[TrashOperation, ...]:
        """Return every safe legacy operation, newest first, without writes."""
        journal_names: list[str] = []
        if snapshot(self._journal_base) is not None:
            assert_plain_chain(self._journal_base)
            _read_snapshot(self._journal_base, "dir")
            try:
                with os.scandir(native(self._journal_base)) as iterator:
                    journal_names = sorted(entry.name for entry in iterator)
            except OSError as exc:
                raise TrashSafetyError(f"无法枚举隔离记录：{exc}") from exc

        operations: list[TrashOperation] = []
        journal_ids: set[str] = set()
        for name in journal_names:
            if not name.endswith(".json"):
                raise TrashSafetyError(f"隔离记录目录包含未知项目：{name}")
            path = canonical(os.path.join(self._journal_base, name))
            _read_snapshot(path, "file")
            operation_id = name[:-5]
            self._valid_operation_id(operation_id)
            data = self._load_journal(operation_id)
            operations.append(self._operation_from_data(data))
            journal_ids.add(operation_id)

        orphaned = self._legacy_backup_operation_ids() - journal_ids
        if orphaned:
            identifiers = "、".join(sorted(item[:8] for item in orphaned)[:3])
            raise TrashSafetyError(
                f"发现缺少认证记录的旧版隔离备份（{identifiers}），已阻止新的写入。"
            )
        operations.sort(key=lambda item: (-item.created_at, item.operation_id))
        return tuple(operations)

    def _assert_no_pending_legacy_operations(self) -> None:
        """Keep 2.0.0 recovery authority intact before a new direct cleanup."""
        pending = tuple(
            operation for operation in self.list_operations()
            if any(record.status != "finalized" for record in operation.records)
        )
        if pending:
            identifiers = "、".join(item.operation_id[:8] for item in pending[:3])
            raise TrashSafetyError(
                f"已有待处理的旧版回收站隔离批次（{identifiers}），"
                "请先恢复或永久清理该批次。"
            )

    def execute(self, plan: TrashPlan, selected_vaults: Iterable[str | Path], *,
                cancel: Event | None = None,
                progress: ProgressCallback = None) -> TrashOperationResult:
        if not _TRASH_TASK_LOCK.acquire(blocking=False):
            return TrashOperationResult(
                "rejected", failures=(TrashFailure(
                    "", "", "lock", "另一个仓库管理任务正在运行，请稍后重试。"
                ),)
            )
        try:
            selected_values = tuple(selected_vaults)
            try:
                selected = self._selected(plan, selected_values)
                self._validate_state_location(selected)
                file_lock = self._acquire_file_lock()
            except (OSError, SyncError) as exc:
                return TrashOperationResult(
                    "rejected", failures=(TrashFailure("", "", "lock", str(exc)),)
                )
            if file_lock is None:
                return TrashOperationResult(
                    "rejected", failures=(TrashFailure(
                        "", "", "lock", "另一个进程正在执行回收站任务，请稍后重试。"
                    ),)
                )
            try:
                return self._execute_locked(
                    plan, selected_values, cancel=cancel, progress=progress
                )
            finally:
                file_lock.release()
        finally:
            _TRASH_TASK_LOCK.release()

    def _execute_locked(self, plan: TrashPlan, selected_vaults: Iterable[str | Path], *,
                        cancel: Event | None = None,
                        progress: ProgressCallback = None) -> TrashOperationResult:
        try:
            selected = self._selected(plan, selected_vaults)
            self._validate_state_location(selected)
            self._assert_no_pending_legacy_operations()
            _check_cancel(cancel)
            self._revalidate_all(selected, cancel, progress)
        except SyncCancelled:
            return TrashOperationResult("cancelled")
        except (OSError, SyncError) as exc:
            return TrashOperationResult(
                "rejected", failures=(TrashFailure("", "", "preflight", str(exc)),)
            )

        results: list[TrashVaultResult] = []
        failures: list[TrashFailure] = []
        cancelled = False
        for index, preview in enumerate(selected):
            if cancel is not None and cancel.is_set():
                cancelled = True
                results.extend(
                    TrashVaultResult(
                        item.vault_root,
                        "cancelled",
                        message="操作已取消；未开始处理该仓库。",
                    )
                    for item in selected[index:]
                )
                break
            try:
                current = _scan_vault(preview.vault_root, cancel, progress)
                if _preview_signature(current) != _preview_signature(preview):
                    raise TrashSafetyError(f"清理前回收站已变化：{preview.vault_root}")
                result, item_failures = _clear_preview(preview, cancel, progress)
            except SyncCancelled:
                cancelled = True
                result = TrashVaultResult(
                    preview.vault_root,
                    "cancelled",
                    message="操作已取消；已删除的项目无法恢复。",
                )
                item_failures = ()
            except (OSError, SyncError) as exc:
                result = TrashVaultResult(
                    preview.vault_root,
                    "failed",
                    message="清理前复核失败；该仓库未开始删除。",
                )
                item_failures = (TrashFailure(
                    preview.vault_root, preview.trash_path, "clear", str(exc)
                ),)
            results.append(result)
            failures.extend(item_failures)
            if result.status == "cancelled":
                cancelled = True
                results.extend(
                    TrashVaultResult(
                        item.vault_root,
                        "cancelled",
                        message="操作已取消；未开始处理该仓库。",
                    )
                    for item in selected[index + 1:]
                )
                break

        if cancelled:
            status = "cancelled"
        elif results and all(item.status == "success" for item in results):
            status = "success"
        else:
            status = "partial"
        return TrashOperationResult(
            status,
            None,
            tuple(results),
            tuple(failures),
            bytes_freed=sum(item.removed_bytes for item in results),
        )

    @staticmethod
    def _journal_selection(data: dict, selected_vaults: Iterable[str | Path] | None) -> list[tuple[int, dict, TrashVaultPreview]]:
        records = [
            (index, value, _preview_from_json(value["preview"]))
            for index, value in enumerate(data["vaults"])
        ]
        if selected_vaults is None:
            return records
        wanted = {_path_key(value) for value in selected_vaults}
        found = {_path_key(preview.vault_root) for _, _, preview in records}
        unknown = wanted - found
        if unknown:
            raise TrashSafetyError("所选仓库不属于该隔离操作。")
        return [item for item in records if _path_key(item[2].vault_root) in wanted]

    def _validate_persisted_state_location(
        self, operation_id: str, selected_vaults: Iterable[str | Path] | None
    ) -> None:
        """Reject a moved/aliased state tree before creating its lock file."""
        data = self._load_journal(operation_id)
        records = self._journal_selection(data, selected_vaults)
        if not records:
            raise TrashSafetyError("没有选择可处理的隔离记录。")
        self._validate_state_location(preview for _, _, preview in records)

    def restore(self, operation_id: str, *, selected_vaults: Iterable[str | Path] | None = None,
                cancel: Event | None = None,
                progress: ProgressCallback = None) -> TrashOperationResult:
        if not _TRASH_TASK_LOCK.acquire(blocking=False):
            return TrashOperationResult(
                "rejected", operation_id, failures=(TrashFailure(
                    "", "", "lock", "另一个仓库管理任务正在运行，请稍后重试。"
                ),)
            )
        try:
            try:
                self._validate_persisted_state_location(operation_id, selected_vaults)
                file_lock = self._acquire_file_lock()
            except (OSError, SyncError) as exc:
                return TrashOperationResult(
                    "rejected", operation_id, failures=(TrashFailure(
                        "", "", "lock", str(exc)
                    ),)
                )
            if file_lock is None:
                return TrashOperationResult(
                    "rejected", operation_id, failures=(TrashFailure(
                        "", "", "lock", "另一个进程正在执行回收站任务，请稍后重试。"
                    ),)
                )
            try:
                return self._restore_locked(
                    operation_id, selected_vaults=selected_vaults,
                    cancel=cancel, progress=progress,
                )
            finally:
                file_lock.release()
        finally:
            _TRASH_TASK_LOCK.release()

    def _restore_locked(self, operation_id: str, *,
                        selected_vaults: Iterable[str | Path] | None = None,
                        cancel: Event | None = None,
                        progress: ProgressCallback = None) -> TrashOperationResult:
        try:
            data = self._load_journal(operation_id)
            records = self._journal_selection(data, selected_vaults)
            if not records:
                raise TrashSafetyError("没有选择可恢复的隔离记录。")
            self._validate_state_location(preview for _, _, preview in records)
            self._verify_operation_root(data, allow_finalized_missing=True)
            prepared: list[tuple[int, dict, TrashVaultPreview, str, tuple[TrashEntry, ...]]] = []
            for index, value, preview in records:
                _check_cancel(cancel)
                if value.get("status") == "finalized":
                    raise TrashSafetyError(f"隔离备份已最终清理，无法恢复：{preview.vault_root}")
                backup, _backup_anchor = self._verify_backup_root(operation_id, value)
                backup_entries, backup_digest = _scan_content(backup, cancel)
                if (_content_signature(backup_entries) != _content_signature(preview.entries)
                        or backup_digest != preview.tree_sha256):
                    raise TrashSafetyError(f"隔离备份内容不完整，拒绝恢复：{preview.vault_root}")
                current = _scan_vault(preview.vault_root, cancel, progress)
                if not _recorded_root_identities_match(current, preview):
                    raise TrashSafetyError(
                        f"仓库或 .trash 已被替换，拒绝恢复：{preview.vault_root}"
                    )
                if current.entries:
                    raise TrashSafetyError(f"目标 .trash 已有内容，拒绝覆盖恢复：{preview.vault_root}")
                prepared.append((index, value, preview, backup, backup_entries))
        except SyncCancelled:
            return TrashOperationResult("cancelled", operation_id)
        except (OSError, SyncError, KeyError, TypeError, ValueError) as exc:
            return TrashOperationResult(
                "rejected", operation_id,
                failures=(TrashFailure("", "", "restore_preflight", str(exc)),),
            )

        results: list[TrashVaultResult] = []
        failures: list[TrashFailure] = []
        cancelled = False
        for position, (index, value, preview, backup, backup_entries) in enumerate(prepared):
            try:
                _check_cancel(cancel)
                current = _scan_vault(preview.vault_root, cancel, progress)
                if not _recorded_root_identities_match(current, preview):
                    raise TrashSafetyError(
                        f"恢复前仓库或 .trash 已被替换：{preview.vault_root}"
                    )
                if current.entries:
                    raise TrashSafetyError(f"恢复前目标 .trash 出现内容：{preview.vault_root}")
                _copy_entries(
                    backup, preview.trash_path, backup_entries,
                    cancel, progress, preview.vault_root, "restore",
                    destination_exists=True,
                )
                restored = _scan_vault(preview.vault_root, cancel, progress)
                if (_content_signature(restored.entries) != _content_signature(preview.entries)
                        or restored.tree_sha256 != preview.tree_sha256):
                    raise TrashSafetyError(f"恢复后内容校验失败：{preview.vault_root}")
                result = TrashVaultResult(
                    preview.vault_root, "success",
                    restored_files=preview.file_count,
                    restored_dirs=preview.dir_count,
                    restored_bytes=preview.total_bytes,
                    message="隔离内容已恢复；备份仍保留，等待最终清理。",
                )
                value["status"] = "restored"
                self._save_journal(data)
            except SyncCancelled:
                cancelled = True
                try:
                    observed = _scan_vault(preview.vault_root)
                    restored_files, restored_dirs, restored_bytes = _matching_restored_counts(
                        observed, preview
                    )
                except (OSError, SyncError):
                    restored_files = restored_dirs = restored_bytes = 0
                result = TrashVaultResult(
                    preview.vault_root, "cancelled",
                    restored_files=restored_files,
                    restored_dirs=restored_dirs,
                    restored_bytes=restored_bytes,
                    message="恢复已取消；隔离备份仍保留。",
                )
                if restored_files or restored_dirs:
                    value["status"] = "restore_partial"
                    try:
                        self._save_journal(data)
                    except OSError as journal_error:
                        failures.append(TrashFailure(
                            preview.vault_root, self._journal_path(operation_id),
                            "journal", str(journal_error),
                        ))
            except (OSError, SyncError) as exc:
                try:
                    observed = _scan_vault(preview.vault_root)
                    restored_files, restored_dirs, restored_bytes = _matching_restored_counts(
                        observed, preview
                    )
                except (OSError, SyncError):
                    restored_files = restored_dirs = restored_bytes = 0
                observed_partial = bool(restored_files or restored_dirs)
                result = TrashVaultResult(
                    preview.vault_root, "partial" if observed_partial else "failed",
                    restored_files=restored_files,
                    restored_dirs=restored_dirs,
                    restored_bytes=restored_bytes,
                    message="恢复不完整；隔离备份仍保留。" if observed_partial else "恢复失败；隔离备份仍保留。",
                )
                failures.append(TrashFailure(
                    preview.vault_root, preview.trash_path, "restore", str(exc)
                ))
                if observed_partial:
                    value["status"] = "restore_partial"
                    try:
                        self._save_journal(data)
                    except OSError as journal_error:
                        failures.append(TrashFailure(
                            preview.vault_root, self._journal_path(operation_id),
                            "journal", str(journal_error),
                        ))
            results.append(result)
            if cancelled:
                results.extend(
                    TrashVaultResult(
                        remaining_preview.vault_root, "cancelled",
                        message="恢复已取消；隔离备份仍保留。",
                    )
                    for _, _, remaining_preview, _, _ in prepared[position + 1:]
                )
                break
        if cancelled:
            status = "cancelled"
        elif all(item.status == "success" for item in results):
            status = "success"
        else:
            status = "partial"
        return TrashOperationResult(status, operation_id, tuple(results), tuple(failures))

    def finalize(self, operation_id: str, *, selected_vaults: Iterable[str | Path] | None = None,
                 cancel: Event | None = None) -> TrashOperationResult:
        if not _TRASH_TASK_LOCK.acquire(blocking=False):
            return TrashOperationResult(
                "rejected", operation_id, failures=(TrashFailure(
                    "", "", "lock", "另一个仓库管理任务正在运行，请稍后重试。"
                ),)
            )
        try:
            try:
                self._validate_persisted_state_location(operation_id, selected_vaults)
                file_lock = self._acquire_file_lock()
            except (OSError, SyncError) as exc:
                return TrashOperationResult(
                    "rejected", operation_id, failures=(TrashFailure(
                        "", "", "lock", str(exc)
                    ),)
                )
            if file_lock is None:
                return TrashOperationResult(
                    "rejected", operation_id, failures=(TrashFailure(
                        "", "", "lock", "另一个进程正在执行回收站任务，请稍后重试。"
                    ),)
                )
            try:
                return self._finalize_locked(
                    operation_id, selected_vaults=selected_vaults, cancel=cancel
                )
            finally:
                file_lock.release()
        finally:
            _TRASH_TASK_LOCK.release()

    def _finalize_locked(self, operation_id: str, *,
                         selected_vaults: Iterable[str | Path] | None = None,
                         cancel: Event | None = None) -> TrashOperationResult:
        try:
            data = self._load_journal(operation_id)
            records = self._journal_selection(data, selected_vaults)
            if not records:
                raise TrashSafetyError("没有选择可最终清理的隔离记录。")
            self._validate_state_location(preview for _, _, preview in records)
            self._verify_operation_root(data, allow_finalized_missing=True)
            prepared: list[tuple[int, dict, TrashVaultPreview, str, PathSnapshot]] = []
            already_finalized: list[TrashVaultResult] = []
            for index, value, preview in records:
                _check_cancel(cancel)
                if value.get("status") == "finalized":
                    already_finalized.append(TrashVaultResult(
                        preview.vault_root, "success", message="隔离备份此前已清理。"
                    ))
                    continue
                backup = self._backup_path(operation_id, str(value["backup_rel"]))
                backup_value = snapshot(backup)
                if backup_value is None:
                    if value.get("status") != "finalizing":
                        raise TrashSafetyError(
                            f"隔离备份目录缺失，拒绝推断已清理：{preview.vault_root}"
                        )
                    value["status"] = "finalized"
                    value["freed_bytes"] = preview.total_bytes
                    self._save_journal(data)
                    already_finalized.append(TrashVaultResult(
                        preview.vault_root, "success",
                        message="已确认此前授权的最终清理已完成。",
                    ))
                    continue
                backup, backup_anchor = self._verify_backup_root(operation_id, value)
                backup_entries, backup_digest = _scan_content(backup, cancel)
                exact = (
                    _content_signature(backup_entries) == _content_signature(preview.entries)
                    and backup_digest == preview.tree_sha256
                )
                if exact:
                    remaining_bytes = preview.total_bytes
                elif value.get("status") in ("finalizing", "finalize_partial"):
                    remaining_bytes = _validated_manifest_subset(
                        backup_entries, preview.entries, require_strict=True
                    )
                else:
                    raise TrashSafetyError(
                        f"隔离备份内容异常，拒绝最终清理：{preview.vault_root}"
                    )
                inferred_freed = preview.total_bytes - remaining_bytes
                if int(value["freed_bytes"]) > inferred_freed:
                    raise TrashSafetyError("隔离备份出现已清理内容回流，拒绝继续。")
                value["freed_bytes"] = inferred_freed
                prepared.append((index, value, preview, backup, backup_anchor))
        except SyncCancelled:
            return TrashOperationResult("cancelled", operation_id)
        except (OSError, SyncError, KeyError, TypeError, ValueError) as exc:
            return TrashOperationResult(
                "rejected", operation_id,
                failures=(TrashFailure("", "", "finalize_preflight", str(exc)),),
            )

        results = already_finalized
        failures: list[TrashFailure] = []
        bytes_freed = 0
        cancelled = False
        for _index, value, preview, backup, backup_anchor in prepared:
            try:
                _check_cancel(cancel)
                # Persist authorization before the first destructive access. If
                # the process dies, a missing backup can only be accepted from
                # this authenticated state and is never treated as a path to
                # delete later.
                value["status"] = "finalizing"
                self._save_journal(data)
                removed_files, removed_dirs, removed_bytes, item_failures, stopped = _remove_backup_tree(
                    backup,
                    cancel,
                    expected_root=backup_anchor,
                    expected_entries=preview.entries,
                )
                bytes_freed += removed_bytes
                for failure in item_failures:
                    failures.append(TrashFailure(
                        preview.vault_root, failure.path, failure.phase, failure.message
                    ))
                if stopped:
                    cancelled = True
                    result = TrashVaultResult(
                        preview.vault_root, "cancelled", removed_files, removed_dirs,
                        removed_bytes, message="最终清理已取消；已释放空间已准确计入。",
                    )
                    value["status"] = "finalize_partial" if (removed_files or removed_dirs) else "finalize_failed"
                elif item_failures:
                    result = TrashVaultResult(
                        preview.vault_root, "partial", removed_files, removed_dirs,
                        removed_bytes, message="隔离备份仅部分清理。",
                    )
                    value["status"] = "finalize_partial" if (removed_files or removed_dirs) else "finalize_failed"
                else:
                    result = TrashVaultResult(
                        preview.vault_root, "success", removed_files, removed_dirs,
                        removed_bytes, message="隔离备份已最终清理。",
                    )
                    value["status"] = "finalized"
                if snapshot(backup) is None:
                    value["freed_bytes"] = preview.total_bytes
                else:
                    remaining_entries, _remaining_digest = _scan_content(backup)
                    remaining_bytes = _validated_manifest_subset(
                        remaining_entries, preview.entries,
                        require_strict=value["status"] == "finalize_partial",
                    )
                    value["freed_bytes"] = preview.total_bytes - remaining_bytes
                self._save_journal(data)
            except SyncCancelled:
                cancelled = True
                result = TrashVaultResult(
                    preview.vault_root, "cancelled", message="最终清理已取消。"
                )
            except (OSError, SyncError) as exc:
                result = TrashVaultResult(
                    preview.vault_root, "failed", message="最终清理失败。"
                )
                failures.append(TrashFailure(
                    preview.vault_root, backup, "finalize", str(exc)
                ))
            results.append(result)
            if cancelled:
                break
        try:
            self._remove_finalized_operation_root(data)
        except (OSError, SyncError) as exc:
            failures.append(TrashFailure(
                "", self._operation_backup_root(operation_id),
                "finalize_root", str(exc),
            ))
        if cancelled:
            status = "cancelled"
        elif not failures and all(item.status == "success" for item in results):
            status = "success"
        else:
            status = "partial"
        return TrashOperationResult(
            status, operation_id, tuple(results), tuple(failures), bytes_freed
        )
