"""Safe, Qt-free deployment of bounded configuration subtrees.

This module replaces the legacy "delete then copytree" scripts with a planned
multi-target transaction.  Every selected target is staged and SHA-256 checked
on its own volume before the first target is switched.  Directory renames keep
the old target available as a durable backup until the caller explicitly calls
``finalize``; ``rollback`` is available from a new engine instance through the
persistent journal.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from ..models import SyncCancelled, SyncError
from ..paths import (assert_plain_chain, canonical, checked_child, identity, native,
                     snapshot, volume_identity)
from .journal import (DeploymentJournal, JournalBatch, JournalError, JournalTarget,
                      OwnedDirectory)


CHUNK_SIZE = 4 * 1024 * 1024
_DEPLOYMENT_TASK_LOCK = threading.Lock()
_OWNED_PREFIX = ".obmanage-deploy-"
_WINDOWS_ACCESS_DENIED = 5
_WINDOWS_READONLY = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_LOCAL_PROCESS_LOCK = threading.Lock()
_LOCAL_PROCESS_KEYS: set[str] = set()
_TERMINAL_BATCH_STATUSES = frozenset({
    "cancelled", "prepare_failed", "rolled_back",
    "rolled_back_with_residuals", "finalized",
})


def _deployment_mutex_name(key: str) -> str:
    """Return a Windows kernel mutex name shared by all login sessions."""
    return "Global\\ObManage.Deployment." + hashlib.sha256(key.encode("utf-8")).hexdigest()


class DeploymentError(SyncError):
    """A bounded deployment could not be completed safely."""


def _canonical_state_dir(path: str | Path) -> str:
    """Resolve existing ancestors before appending a possibly missing suffix."""
    requested = canonical(path)
    assert_plain_chain(requested)
    current = requested
    while snapshot(current) is None:
        parent = canonical(os.path.dirname(current))
        if parent == current:
            raise DeploymentError(f"事务状态目录所在磁盘不可用：{requested}")
        current = parent
    resolved_anchor = canonical(os.path.realpath(native(current)))
    relative = os.path.relpath(requested, current)
    resolved = (resolved_anchor if relative == "."
                else canonical(os.path.join(resolved_anchor, relative)))
    assert_plain_chain(resolved)
    return resolved


class _InterprocessLockSet:
    """Crash-released process locks for one state directory and target set."""

    def __init__(self, state_dir: str, target_roots: Iterable[str],
                 source_roots: Iterable[str] = ()):
        targets = tuple(target_roots)
        sources = tuple(source_roots)
        values = [f"state:{_path_key(state_dir)}"]
        values.extend(f"target:{_path_key(root)}" for root in targets)
        self.keys = tuple(sorted(set(values)))
        self.state_dir = state_dir
        self.target_roots = targets
        self.source_roots = sources
        self.handles: list[Any] = []
        self.local_keys: tuple[str, ...] = ()

    def acquire(self) -> None:
        with _LOCAL_PROCESS_LOCK:
            if any(key in _LOCAL_PROCESS_KEYS for key in self.keys):
                raise DeploymentError("另一个 ObManage 进程正在操作相同状态目录或目标仓库。")
            _LOCAL_PROCESS_KEYS.update(self.keys)
            self.local_keys = self.keys
        try:
            if os.name == "nt":
                self._acquire_windows()
            else:
                self._acquire_posix()
        except BaseException:
            self.release()
            raise

    def _acquire_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        wait = kernel.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        for key in self.keys:
            name = _deployment_mutex_name(key)
            handle = create_mutex(None, False, name)
            if not handle:
                raise DeploymentError(f"无法创建跨进程任务锁：{ctypes.WinError(ctypes.get_last_error())}")
            result = wait(handle, 0)
            if result not in (0x00000000, 0x00000080):  # WAIT_OBJECT_0 / WAIT_ABANDONED
                kernel.CloseHandle(handle)
                if result == 0x00000102:  # WAIT_TIMEOUT
                    raise DeploymentError("另一个 ObManage 进程正在操作相同状态目录或目标仓库。")
                raise DeploymentError("无法获取跨进程任务锁。")
            self.handles.append(("windows", handle))

    def _acquire_posix(self) -> None:
        import fcntl

        directory = canonical(os.path.join(tempfile.gettempdir(), "obmanage-deployment-locks"))
        protected_roots = (*self.source_roots, *self.target_roots)
        if any(_same_or_contains(root, directory) or _same_or_contains(directory, root)
               for root in protected_roots):
            directory = canonical(os.path.join(self.state_dir, "deployment-locks"))
        assert_plain_chain(directory)
        os.makedirs(native(directory), mode=0o700, exist_ok=True)
        for key in self.keys:
            path = canonical(os.path.join(directory,
                              hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock"))
            descriptor = os.open(native(path), os.O_RDWR | os.O_CREAT,
                                 stat.S_IRUSR | stat.S_IWUSR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(descriptor)
                raise DeploymentError(
                    "另一个 ObManage 进程正在操作相同状态目录或目标仓库。"
                ) from exc
            self.handles.append(("posix", descriptor))

    def release(self) -> None:
        for kind, handle in reversed(self.handles):
            try:
                if kind == "windows":
                    import ctypes
                    from ctypes import wintypes
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    release_mutex = kernel.ReleaseMutex
                    release_mutex.argtypes = [wintypes.HANDLE]
                    release_mutex.restype = wintypes.BOOL
                    close_handle = kernel.CloseHandle
                    close_handle.argtypes = [wintypes.HANDLE]
                    close_handle.restype = wintypes.BOOL
                    release_mutex(handle)
                    close_handle(handle)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    os.close(handle)
            except OSError:
                pass
        self.handles.clear()
        with _LOCAL_PROCESS_LOCK:
            for key in self.local_keys:
                _LOCAL_PROCESS_KEYS.discard(key)
        self.local_keys = ()


@dataclass(frozen=True)
class DeploymentComponent:
    """A source component and its bounded destination inside a selected vault.

    ``source_subtree`` lets a page accept either a component directory itself or
    a containing vault/suite.  The convenience constructors encode the legacy
    ``.obsidian`` and ``File/Templater`` resolution rules.
    """

    component_id: str
    source: str
    destination: str
    source_subtree: str = ""

    @classmethod
    def direct(cls, component_id: str, source: str | Path,
               destination: str) -> "DeploymentComponent":
        return cls(component_id, os.fspath(source), destination)

    @classmethod
    def subtree(cls, component_id: str, source: str | Path, source_subtree: str,
                destination: str | None = None) -> "DeploymentComponent":
        return cls(component_id, os.fspath(source), destination or source_subtree, source_subtree)

    @classmethod
    def obsidian(cls, source: str | Path, component_id: str = "obsidian") -> "DeploymentComponent":
        return cls(component_id, os.fspath(source), ".obsidian", ".obsidian")

    @classmethod
    def templater(cls, source: str | Path, component_id: str = "templater") -> "DeploymentComponent":
        return cls(component_id, os.fspath(source), "File/Templater", "File/Templater")


@dataclass(frozen=True)
class DeploymentTarget:
    """One explicitly selected repository root."""

    target_id: str
    root: str


@dataclass(frozen=True)
class DeploymentSelection:
    """An explicit component-to-target pairing."""

    component: DeploymentComponent
    target: DeploymentTarget


@dataclass(frozen=True)
class DeploymentRequest:
    selections: tuple[DeploymentSelection, ...]
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "selections", tuple(self.selections))


@dataclass(frozen=True)
class FileChange:
    action: str
    relative_path: str
    size: int = 0
    reason: str = ""
    kind: str = "file"
    will_replace_entity: bool = False


@dataclass(frozen=True)
class DeploymentProgress:
    phase: str
    message: str = ""
    batch_id: str = ""
    selection_id: str = ""
    component_id: str = ""
    target_id: str = ""
    relative_path: str = ""
    completed_files: int = 0
    total_files: int = 0
    completed_bytes: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class EntrySnapshot:
    kind: str
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int
    sha256: str | None = None

    @classmethod
    def from_state(cls, state: dict[str, Any], digest: str | None = None) -> "EntrySnapshot":
        return cls(
            kind=str(state["kind"]), size=int(state["size"]),
            mtime_ns=int(state["mtime_ns"]), ctime_ns=int(state["ctime_ns"]),
            inode=int(state["inode"]), device=int(state["device"]), sha256=digest,
        )

    @property
    def identity(self) -> tuple[str, int, int]:
        return self.kind, self.device, self.inode


@dataclass(frozen=True)
class TreeEntry:
    relative_path: str
    snapshot: EntrySnapshot


@dataclass(frozen=True)
class FrozenTree:
    root: str
    volume: str
    root_snapshot: EntrySnapshot
    entries: tuple[TreeEntry, ...]

    @property
    def files(self) -> tuple[TreeEntry, ...]:
        return tuple(item for item in self.entries if item.snapshot.kind == "file")

    @property
    def total_bytes(self) -> int:
        return sum(item.snapshot.size for item in self.files)

    def manifest(self) -> dict[str, dict[str, Any]]:
        value: dict[str, dict[str, Any]] = {"": {"kind": "dir"}}
        for item in self.entries:
            state = item.snapshot
            if state.kind == "dir":
                value[item.relative_path] = {"kind": "dir"}
            else:
                value[item.relative_path] = {
                    "kind": "file", "size": state.size, "sha256": state.sha256,
                }
        return value


@dataclass(frozen=True)
class ParentGuard:
    desired_path: str
    actual_path: str | None
    snapshot: EntrySnapshot | None


@dataclass(frozen=True)
class DeploymentTargetPlan:
    selection_id: str
    component_id: str
    target_id: str
    source_path: str
    source_authorization_root: str
    target_root: str
    target_path: str
    existing_target_path: str | None
    changes: tuple[FileChange, ...]
    needs_deploy: bool
    source_tree: FrozenTree = field(repr=False)
    target_tree: FrozenTree | None = field(repr=False)
    target_volume: str = field(repr=False)
    target_root_snapshot: EntrySnapshot = field(repr=False)
    parent_guards: tuple[ParentGuard, ...] = field(repr=False)

    @property
    def counts(self) -> dict[str, int]:
        return {name: sum(change.action == name for change in self.changes)
                for name in ("add", "update", "delete", "skip")}

    @property
    def file_counts(self) -> dict[str, int]:
        return {name: sum(change.action == name and change.kind == "file"
                          for change in self.changes)
                for name in ("add", "update", "delete", "skip")}

    @property
    def directory_counts(self) -> dict[str, int]:
        return {name: sum(change.action == name and change.kind == "dir"
                          for change in self.changes)
                for name in ("add", "update", "delete", "skip")}

    @property
    def replaces_tree(self) -> bool:
        """Whether commit will replace the target root and all child entities."""
        return self.needs_deploy


@dataclass(frozen=True)
class DeploymentPlan:
    batch_id: str
    label: str
    targets: tuple[DeploymentTargetPlan, ...]
    created_at: float
    seal: str = field(repr=False)

    @property
    def changes(self) -> tuple[FileChange, ...]:
        return tuple(change for target in self.targets for change in target.changes)

    @property
    def needs_deploy(self) -> bool:
        return any(target.needs_deploy for target in self.targets)


@dataclass(frozen=True)
class DeploymentResult:
    batch_id: str
    status: str
    journal_status: str = ""
    prepared_targets: int = 0
    committed_targets: int = 0
    rolled_back_targets: int = 0
    errors: tuple[str, ...] = ()
    duration_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return self.status == "success"


ProgressCallback = Callable[[DeploymentProgress], None] | None


def _cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("已取消限定子树分发。")


def _emit(callback: ProgressCallback, phase: str, **values: Any) -> None:
    if callback is not None:
        callback(DeploymentProgress(phase=phase, **values))


def _case_key(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.normpath(canonical(value))).casefold()


def _relative_parts(value: str, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentError(f"{field_name}不能为空。")
    normalized = value.replace("\\", "/")
    parts = tuple(normalized.split("/"))
    if (any(part in ("", ".", "..") for part in parts)
            or os.path.isabs(value)
            or any(":" in part for part in parts)):
        raise DeploymentError(f"{field_name}必须是限定子树内的相对路径：{value}")
    if _case_key(parts[0]).startswith(_OWNED_PREFIX.casefold()):
        raise DeploymentError(f"{field_name}使用了 ObManage 保留名称。")
    return parts


def _entry_state(path: str) -> EntrySnapshot | None:
    state = snapshot(path)
    return EntrySnapshot.from_state(state) if state is not None else None


def _entry_matches(actual: EntrySnapshot | None, expected: EntrySnapshot | None,
                   *, digest: bool = False) -> bool:
    """Compare stable file identity without treating read-triggered ctime as a write.

    On Windows an immediately reopened newly-created file can receive a deferred
    ctime update even though its content, name and mtime are unchanged.  Content
    deployments therefore bind files by kind, volume/file ID, size, mtime and
    SHA-256; ctime remains recorded for diagnostics but is not sole authority.
    """
    if actual is None or expected is None:
        return actual is expected
    base = (actual.kind, actual.size, actual.mtime_ns, actual.inode, actual.device)
    wanted = (expected.kind, expected.size, expected.mtime_ns, expected.inode, expected.device)
    return base == wanted and (not digest or actual.sha256 == expected.sha256)


def _directory_guard_matches(actual: EntrySnapshot | None,
                             expected: EntrySnapshot | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return (
        actual.kind, actual.size, actual.mtime_ns, actual.ctime_ns,
        actual.inode, actual.device,
    ) == (
        expected.kind, expected.size, expected.mtime_ns, expected.ctime_ns,
        expected.inode, expected.device,
    )


def _tree_matches(actual: FrozenTree, expected: FrozenTree) -> bool:
    if (canonical(actual.root) != canonical(expected.root)
            or actual.volume != expected.volume
            or actual.root_snapshot.identity != expected.root_snapshot.identity):
        return False
    if len(actual.entries) != len(expected.entries):
        return False
    return all(
        first.relative_path == second.relative_path
        and _entry_matches(first.snapshot, second.snapshot, digest=True)
        for first, second in zip(actual.entries, expected.entries)
    )


def _same_or_contains(first: str, second: str) -> bool:
    first_key, second_key = _path_key(first), _path_key(second)
    try:
        return _path_key(os.path.commonpath((first_key, second_key))) == first_key
    except ValueError:
        return False


def _find_case_child(parent: str, name: str) -> str | None:
    """Find one Windows-equivalent child and reject ambiguous case aliases."""
    try:
        with os.scandir(native(parent)) as iterator:
            matches = [entry.name for entry in iterator if entry.name.casefold() == name.casefold()]
    except OSError as exc:
        raise DeploymentError(f"无法检查目录路径：{parent}（{exc}）") from exc
    if len(matches) > 1:
        raise DeploymentError(f"存在 Windows 无法区分的大小写重名路径：{parent} / {name}")
    return canonical(os.path.join(parent, matches[0])) if matches else None


def _resolve_relative_existing(root: str, parts: tuple[str, ...]) -> tuple[str, tuple[ParentGuard, ...]]:
    """Resolve existing casing; return the desired final path and parent guards."""
    current_actual = root
    desired = root
    guards: list[ParentGuard] = []
    missing = False
    for index, part in enumerate(parts):
        desired = canonical(os.path.join(desired, part))
        is_final = index == len(parts) - 1
        if missing:
            actual = None
        else:
            actual = _find_case_child(current_actual, part)
            if actual is None:
                missing = True
            else:
                current_actual = actual
        if not is_final:
            state = _entry_state(actual) if actual else None
            if state is not None and state.kind != "dir":
                raise DeploymentError(f"目标父路径不是普通目录：{actual}")
            guards.append(ParentGuard(desired, actual, state))
    return (current_actual if not missing else ""), tuple(guards)


def _ascend(path: str, count: int) -> str:
    value = path
    for _ in range(count):
        value = canonical(os.path.dirname(value))
    return value


def _resolve_component_source(component: DeploymentComponent) -> tuple[str, str]:
    if not component.component_id.strip():
        raise DeploymentError("组件编号不能为空。")
    if not component.source.strip():
        raise DeploymentError(f"组件 {component.component_id} 的来源不能为空。")
    start = canonical(component.source)
    assert_plain_chain(start)
    start = canonical(os.path.realpath(native(start)))
    assert_plain_chain(start)
    authorization_root = start
    if not component.source_subtree:
        candidate = start
        destination_parts = _relative_parts(component.destination, field_name="目标子目录")
        start_parts = tuple(Path(start).parts)
        if (len(start_parts) >= len(destination_parts)
                and tuple(part.casefold() for part in start_parts[-len(destination_parts):])
                == tuple(part.casefold() for part in destination_parts)):
            authorization_root = _ascend(start, len(destination_parts))
    else:
        parts = _relative_parts(component.source_subtree, field_name="来源子目录")
        start_parts = tuple(Path(start).parts)
        folded_start = tuple(part.casefold() for part in start_parts)
        folded_subtree = tuple(part.casefold() for part in parts)
        candidate: str | None = None
        # Direct component path (including File/Templater itself).
        if len(folded_start) >= len(folded_subtree) and folded_start[-len(parts):] == folded_subtree:
            candidate = start
            authorization_root = _ascend(start, len(parts))
        elif start_parts and start_parts[-1].casefold() == parts[-1].casefold():
            candidate = start
            authorization_root = _ascend(start, 1)
        else:
            # A containing File directory is also valid for File/Templater.
            suffix_length = 0
            for length in range(min(len(parts), len(start_parts)), 0, -1):
                if folded_start[-length:] == folded_subtree[:length]:
                    suffix_length = length
                    break
            if suffix_length:
                authorization_root = _ascend(start, suffix_length)
            current = start
            for part in parts[suffix_length:]:
                found = _find_case_child(current, part)
                current = found or canonical(os.path.join(current, part))
            candidate = current
    assert_plain_chain(candidate)
    candidate = canonical(os.path.realpath(native(candidate)))
    state = snapshot(candidate)
    if state is None or state["kind"] != "dir":
        raise DeploymentError(f"组件来源子目录不存在：{candidate}")
    authorization_root = canonical(os.path.realpath(native(authorization_root)))
    assert_plain_chain(authorization_root)
    if not _same_or_contains(authorization_root, candidate):
        raise DeploymentError("组件来源超出用户选定的来源范围。")
    return candidate, authorization_root


def _hash_file(path: str, expected: EntrySnapshot,
               cancel: threading.Event | None) -> str:
    actual = _entry_state(path)
    if not _entry_matches(actual, expected):
        raise DeploymentError(f"文件在读取前发生变化：{path}")
    digest = hashlib.sha256()
    try:
        with open(native(path), "rb") as stream:
            opened = os.fstat(stream.fileno())
            opened_tuple = (
                opened.st_size, opened.st_mtime_ns, opened.st_ino, opened.st_dev,
            )
            expected_tuple = (
                expected.size, expected.mtime_ns, expected.inode, expected.device,
            )
            if opened_tuple != expected_tuple or not stat.S_ISREG(opened.st_mode):
                raise DeploymentError(f"打开文件时内容身份已改变：{path}")
            while True:
                _cancelled(cancel)
                block = stream.read(CHUNK_SIZE)
                if not block:
                    break
                digest.update(block)
            finished = os.fstat(stream.fileno())
            if (finished.st_size, finished.st_mtime_ns,
                    finished.st_ino, finished.st_dev) != opened_tuple:
                raise DeploymentError(f"文件在读取期间发生变化：{path}")
    except OSError as exc:
        raise DeploymentError(f"无法读取文件：{path}（{exc}）") from exc
    if not _entry_matches(_entry_state(path), expected):
        raise DeploymentError(f"文件在读取后发生变化：{path}")
    return digest.hexdigest()


def _scan_tree(root: str, cancel: threading.Event | None,
               progress: ProgressCallback = None, *, hash_files: bool = True,
               progress_values: dict[str, Any] | None = None) -> FrozenTree:
    _cancelled(cancel)
    assert_plain_chain(root)
    root_state = _entry_state(root)
    if root_state is None or root_state.kind != "dir":
        raise DeploymentError(f"目录不存在或不是普通目录：{root}")
    volume = volume_identity(root)
    raw: list[tuple[str, EntrySnapshot]] = []
    seen: dict[str, str] = {}
    stack = [(root, "")]
    directory_guards: list[
        tuple[str, EntrySnapshot, tuple[tuple[str, EntrySnapshot], ...]]
    ] = []
    while stack:
        _cancelled(cancel)
        folder, prefix = stack.pop()
        assert_plain_chain(folder)
        folder_before = _entry_state(folder)
        if folder_before is None or folder_before.kind != "dir":
            raise DeploymentError(f"扫描期间目录被移除或替换：{folder}")
        try:
            with os.scandir(native(folder)) as iterator:
                entries = sorted(tuple(iterator), key=lambda item: (item.name.casefold(), item.name))
        except OSError as exc:
            raise DeploymentError(f"无法完整扫描目录：{folder}（{exc}）") from exc
        direct_children: list[tuple[str, EntrySnapshot]] = []
        for entry in entries:
            _cancelled(cancel)
            relative = prefix + entry.name
            state = _entry_state(canonical(entry.path))
            if state is None:
                raise DeploymentError(f"扫描期间项目消失：{relative}")
            if state.kind not in ("file", "dir"):
                raise DeploymentError(f"不支持链接、重解析点或特殊项目：{relative}")
            key = _case_key(relative)
            if key in seen:
                raise DeploymentError(
                    f"存在 Windows 无法区分的大小写重名路径：{seen[key]} / {relative}"
                )
            seen[key] = relative
            raw.append((relative, state))
            direct_children.append((entry.name, state))
            if state.kind == "dir":
                stack.append((canonical(entry.path), relative + "/"))
        folder_after = _entry_state(folder)
        if not _directory_guard_matches(folder_after, folder_before):
            raise DeploymentError(f"枚举期间目录发生变化：{folder}")
        directory_guards.append((folder, folder_after, tuple(direct_children)))
    items: list[TreeEntry] = []
    progress_values = progress_values or {}
    total_files = sum(state.kind == "file" for _, state in raw)
    completed = 0
    for relative, state in sorted(raw, key=lambda item: (_case_key(item[0]), item[0])):
        digest = None
        if state.kind == "file" and hash_files:
            digest = _hash_file(checked_child(root, relative), state, cancel)
            completed += 1
            _emit(progress, "analyze", relative_path=relative, completed_files=completed,
                  total_files=total_files, message="正在生成完整内容摘要", **progress_values)
        items.append(TreeEntry(relative, replace(state, sha256=digest)))
    # A second stable name/identity enumeration closes the window between the
    # first directory listing and later file hashing.  In particular, an entry
    # created while a previously listed file is being read cannot be omitted
    # from a supposedly current plan.
    for folder, expected_folder, expected_children in directory_guards:
        _cancelled(cancel)
        assert_plain_chain(folder)
        final_before = _entry_state(folder)
        if not _directory_guard_matches(final_before, expected_folder):
            raise DeploymentError(f"扫描期间目录发生变化：{folder}")
        try:
            with os.scandir(native(folder)) as iterator:
                final_entries = sorted(
                    tuple(iterator), key=lambda item: (item.name.casefold(), item.name)
                )
        except OSError as exc:
            raise DeploymentError(f"无法复核目录枚举：{folder}（{exc}）") from exc
        final_children: list[tuple[str, EntrySnapshot]] = []
        for entry in final_entries:
            state = _entry_state(canonical(entry.path))
            if state is None or state.kind not in ("file", "dir"):
                raise DeploymentError(f"复核期间项目消失或变成特殊项：{entry.name}")
            final_children.append((entry.name, state))
        final_after = _entry_state(folder)
        if (not _directory_guard_matches(final_after, final_before)
                or len(final_children) != len(expected_children)
                or any(
                    actual_name != expected_name
                    or not _entry_matches(actual_state, expected_state)
                    for (actual_name, actual_state), (expected_name, expected_state)
                    in zip(final_children, expected_children)
                )):
            raise DeploymentError(f"扫描期间目录枚举发生变化：{folder}")
    if not _directory_guard_matches(_entry_state(root), root_state):
        raise DeploymentError(f"扫描期间目录发生变化：{root}")
    return FrozenTree(root, volume, root_state, tuple(items))


def _portable_equal(first: FrozenTree | None, second: FrozenTree | None) -> bool:
    if first is None or second is None:
        return first is second
    return first.manifest() == second.manifest()


def _compare_entries(source: FrozenTree, target: FrozenTree | None) -> tuple[FileChange, ...]:
    source_entries = {_case_key(item.relative_path): item for item in source.entries}
    target_entries = ({_case_key(item.relative_path): item for item in target.entries}
                      if target is not None else {})
    changes: list[FileChange] = []
    if target is None:
        changes.append(FileChange("add", "", 0, "限定目标根目录不存在", kind="dir"))
    for key in sorted(set(source_entries) | set(target_entries)):
        source_item, target_item = source_entries.get(key), target_entries.get(key)
        if source_item is None:
            changes.append(FileChange("delete", target_item.relative_path, target_item.snapshot.size,
                                      "目标中存在、来源中不存在", kind=target_item.snapshot.kind))
        elif target_item is None:
            changes.append(FileChange("add", source_item.relative_path, source_item.snapshot.size,
                                      "来源中的新项目", kind=source_item.snapshot.kind))
        elif source_item.snapshot.kind != target_item.snapshot.kind:
            changes.append(FileChange(
                "update", source_item.relative_path, source_item.snapshot.size,
                f"项目类型由 {target_item.snapshot.kind} 变为 {source_item.snapshot.kind}",
                kind=source_item.snapshot.kind,
            ))
        elif source_item.snapshot.kind == "file" and (
                source_item.relative_path != target_item.relative_path
                or source_item.snapshot.sha256 != target_item.snapshot.sha256):
            reason = ("文件名大小写不同" if source_item.snapshot.sha256 == target_item.snapshot.sha256
                      else "完整内容不同")
            changes.append(FileChange("update", source_item.relative_path,
                                      source_item.snapshot.size, reason, kind="file"))
        elif source_item.snapshot.kind == "dir" and source_item.relative_path != target_item.relative_path:
            changes.append(FileChange("update", source_item.relative_path, 0,
                                      "目录名大小写不同", kind="dir"))
        else:
            changes.append(FileChange("skip", source_item.relative_path,
                                      source_item.snapshot.size,
                                      "完整内容相同" if source_item.snapshot.kind == "file" else "目录已存在",
                                      kind=source_item.snapshot.kind))
    needs_deploy = not _portable_equal(source, target)
    if needs_deploy:
        changes = [replace(
            change,
            reason=(change.reason + "；整棵限定子树提交时会替换此项目实体"
                    if change.action == "skip" else change.reason),
            will_replace_entity=True,
        ) for change in changes]
    return tuple(changes)


def _tree_payload(tree: FrozenTree | None) -> Any:
    if tree is None:
        return None
    return {
        "root": tree.root, "volume": tree.volume,
        "root_snapshot": tree.root_snapshot.__dict__,
        "entries": [(item.relative_path, item.snapshot.__dict__) for item in tree.entries],
    }


def _plan_payload(batch_id: str, label: str,
                  targets: Iterable[DeploymentTargetPlan], created_at: float) -> dict[str, Any]:
    return {
        "batch_id": batch_id, "label": label, "created_at": created_at,
        "targets": [{
            "selection_id": item.selection_id,
            "component_id": item.component_id,
            "target_id": item.target_id,
            "source_path": item.source_path,
            "source_authorization_root": item.source_authorization_root,
            "target_root": item.target_root,
            "target_path": item.target_path,
            "existing_target_path": item.existing_target_path,
            "changes": [change.__dict__ for change in item.changes],
            "needs_deploy": item.needs_deploy,
            "source_tree": _tree_payload(item.source_tree),
            "target_tree": _tree_payload(item.target_tree),
            "target_volume": item.target_volume,
            "target_root_snapshot": item.target_root_snapshot.__dict__,
            "parent_guards": [{
                "desired_path": guard.desired_path,
                "actual_path": guard.actual_path,
                "snapshot": guard.snapshot.__dict__ if guard.snapshot else None,
            } for guard in item.parent_guards],
        } for item in targets],
    }


def _seal_plan(batch_id: str, label: str, targets: tuple[DeploymentTargetPlan, ...],
               created_at: float) -> str:
    encoded = json.dumps(_plan_payload(batch_id, label, targets, created_at),
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identity_tuple(state: EntrySnapshot | None) -> tuple[str, int, int] | None:
    return state.identity if state is not None else None


def _journal_target(plan: DeploymentTargetPlan) -> JournalTarget:
    relative = os.path.relpath(plan.target_path, plan.target_root).replace("\\", "/")
    return JournalTarget(
        selection_id=plan.selection_id,
        component_id=plan.component_id,
        source_path=plan.source_path,
        source_authorization_root=plan.source_authorization_root,
        source_volume=plan.source_tree.volume,
        target_root=plan.target_root,
        target_relative=relative,
        target_path=plan.target_path,
        target_volume=plan.target_volume,
        target_root_identity=plan.target_root_snapshot.identity,
        phase="planned" if plan.needs_deploy else "unchanged",
        original_identity=_identity_tuple(plan.target_tree.root_snapshot if plan.target_tree else None),
        source_manifest=plan.source_tree.manifest(),
        original_manifest=plan.target_tree.manifest() if plan.target_tree else None,
        deployed_manifest=plan.source_tree.manifest(),
    )


def _assert_identity(path: str, expected: tuple[str, int, int] | None,
                     description: str) -> None:
    actual = identity(snapshot(path))
    if actual != expected:
        raise DeploymentError(f"{description}身份已改变：{path}")


def _manifest_tree(root: str, cancel: threading.Event | None = None) -> tuple[
        dict[str, dict[str, Any]], tuple[str, int, int]]:
    tree = _scan_tree(root, cancel, None, hash_files=True)
    return tree.manifest(), tree.root_snapshot.identity


def _assert_manifest(root: str, expected: dict[str, dict[str, Any]],
                     expected_identity: tuple[str, int, int] | None,
                     description: str, *, allow_subset: bool = False,
                     allow_incomplete_files: bool = False) -> FrozenTree:
    actual_tree = _scan_tree(root, None, None, hash_files=True)
    actual = actual_tree.manifest()
    root_identity = actual_tree.root_snapshot.identity
    if expected_identity is not None and root_identity != expected_identity:
        raise DeploymentError(f"{description}目录身份已改变：{root}")
    if allow_subset:
        # An interrupted write can leave a shorter file with a different hash.
        # The UUID path and root identity establish ownership; path and kind
        # membership ensure cleanup never expands beyond the planned stage.
        if any(
            relative not in expected
            or expected[relative]["kind"] != item["kind"]
            or (item["kind"] == "file" and not allow_incomplete_files
                and expected[relative] != item)
            for relative, item in actual.items()
        ):
            raise DeploymentError(f"{description}包含未登记或已改变的内容：{root}")
    elif actual != expected:
        raise DeploymentError(f"{description}内容已改变：{root}")
    return actual_tree


def _remove_owned_tree(root: str, expected: dict[str, dict[str, Any]],
                       expected_identity: tuple[str, int, int] | None,
                       description: str, *, allow_subset: bool = False,
                       allow_incomplete_files: bool = False) -> None:
    """Delete a verified owned tree without recursive traversal during deletion."""
    # Use the exact tree returned by the authenticated manifest validation.
    # A later unknown entry is never added to the deletion set; at worst it
    # prevents the final rmdir and leaves the transaction safely retryable.
    actual_tree = _assert_manifest(
        root, expected, expected_identity, description,
        allow_subset=allow_subset,
        allow_incomplete_files=allow_incomplete_files,
    )
    deletion_guard = _scan_tree(root, None, None, hash_files=False)
    if (deletion_guard.root_snapshot.identity != actual_tree.root_snapshot.identity
            or len(deletion_guard.entries) != len(actual_tree.entries)
            or any(
                current.relative_path != authorized.relative_path
                or not _entry_matches(current.snapshot, authorized.snapshot)
                for current, authorized
                in zip(deletion_guard.entries, actual_tree.entries)
            )):
        raise DeploymentError(f"{description}在清理前出现未登记或变化的内容：{root}")
    files = [entry for entry in actual_tree.entries if entry.snapshot.kind == "file"]
    directories = [entry for entry in actual_tree.entries if entry.snapshot.kind == "dir"]
    for entry in sorted(files, key=lambda item: (-item.relative_path.count("/"),
                                                  _case_key(item.relative_path))):
        relative = entry.relative_path
        path = checked_child(root, relative)
        state = _entry_state(path)
        if not _entry_matches(state, entry.snapshot):
            raise DeploymentError(f"清理前文件已改变：{path}")
        _unlink_owned_file(path, entry.snapshot)
    for entry in sorted(directories, key=lambda item: (-item.relative_path.count("/"),
                                                        _case_key(item.relative_path))):
        relative = entry.relative_path
        path = checked_child(root, relative)
        state = _entry_state(path)
        if state is None or state.identity != entry.snapshot.identity:
            raise DeploymentError(f"清理前目录已改变：{path}")
        _rmdir_owned(path, entry.snapshot.identity)
    _assert_identity(root, expected_identity, description)
    _rmdir_owned(root, expected_identity)


def _owned_file_stat_matches(value: os.stat_result, expected: EntrySnapshot) -> bool:
    return (stat.S_ISREG(value.st_mode)
            and not getattr(value, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
            and value.st_nlink == 1
            and (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) == (
                expected.device, expected.inode, expected.size, expected.mtime_ns
            ))


def _unlink_owned_file(path: str, expected: EntrySnapshot) -> None:
    """Remove one owned file, narrowly retrying a verified DOS read-only file."""
    try:
        os.unlink(native(path))
        return
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != _WINDOWS_ACCESS_DENIED:
            raise
    current = os.lstat(native(path))
    if (not _owned_file_stat_matches(current, expected)
            or not getattr(current, "st_file_attributes", 0) & _WINDOWS_READONLY):
        raise DeploymentError(f"只读清理前文件身份已改变：{path}")
    # Re-read content immediately before changing the DOS bit.  The containing
    # tree was already validated, but this closes the per-file mutation window.
    if _hash_file(path, expected, None) != expected.sha256:
        raise DeploymentError(f"只读清理前文件内容已改变：{path}")
    os.chmod(native(path), stat.S_IWRITE)
    writable = _entry_state(path)
    if not _entry_matches(writable, expected):
        if writable is not None and writable.identity == expected.identity:
            try:
                os.chmod(native(path), stat.S_IREAD)
            except OSError:
                pass
        raise DeploymentError(f"解除只读属性后文件身份已改变：{path}")
    try:
        os.unlink(native(path))
    except BaseException:
        if _entry_matches(_entry_state(path), writable):
            try:
                os.chmod(native(path), stat.S_IREAD)
            except OSError:
                pass
        raise


def _rmdir_owned(path: str, expected_identity: tuple[str, int, int] | None) -> None:
    """Remove one verified empty owned directory, including a DOS read-only one."""
    try:
        os.rmdir(native(path))
        return
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != _WINDOWS_ACCESS_DENIED:
            raise
    current = os.lstat(native(path))
    current_identity = ("dir", current.st_dev, current.st_ino)
    if (not stat.S_ISDIR(current.st_mode)
            or getattr(current, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
            or current_identity != expected_identity
            or not getattr(current, "st_file_attributes", 0) & _WINDOWS_READONLY):
        raise DeploymentError(f"只读清理前目录身份已改变：{path}")
    os.chmod(native(path), stat.S_IWRITE)
    if identity(snapshot(path)) != expected_identity:
        try:
            os.chmod(native(path), stat.S_IREAD)
        except OSError:
            pass
        raise DeploymentError(f"解除只读属性后目录身份已改变：{path}")
    try:
        os.rmdir(native(path))
    except BaseException:
        if identity(snapshot(path)) == expected_identity:
            try:
                os.chmod(native(path), stat.S_IREAD)
            except OSError:
                pass
        raise


def _rename_directory(source: str, target: str) -> None:
    os.rename(native(source), native(target))


class DeploymentEngine:
    """Plan, stage, atomically switch, undo, and finalize subtree deployments."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = _canonical_state_dir(state_dir)
        self.journal = DeploymentJournal(self.state_dir)
        self._lock = _DEPLOYMENT_TASK_LOCK

    def _check_state_location(self, sources: Iterable[str], target_roots: Iterable[str]) -> None:
        assert_plain_chain(self.state_dir)
        for root in (*tuple(sources), *tuple(target_roots)):
            if _same_or_contains(root, self.state_dir) or _same_or_contains(self.state_dir, root):
                raise DeploymentError("部署事务状态目录必须与来源和目标仓库分离。")

    def analyze(self, request: DeploymentRequest, cancel: threading.Event | None = None,
                progress: ProgressCallback = None) -> DeploymentPlan:
        if not self._lock.acquire(blocking=False):
            raise DeploymentError("已有仓库管理任务正在运行。")
        try:
            _cancelled(cancel)
            if not isinstance(request, DeploymentRequest) or not request.selections:
                raise DeploymentError("请明确选择至少一个组件和目标仓库。")
            batch_id = str(uuid.uuid4())
            resolved: list[
                tuple[DeploymentSelection, str, str, str, str, tuple[ParentGuard, ...]]
            ] = []
            source_paths: list[str] = []
            source_authorization_roots: list[str] = []
            target_roots: list[str] = []
            logical_targets: list[str] = []
            target_ids: set[tuple[str, str]] = set()
            for selection in request.selections:
                _cancelled(cancel)
                if not selection.target.target_id.strip():
                    raise DeploymentError("目标编号不能为空。")
                source_path, source_authorization_root = _resolve_component_source(
                    selection.component
                )
                destination_parts = _relative_parts(selection.component.destination, field_name="目标子目录")
                target_root_input = canonical(selection.target.root)
                if not selection.target.root.strip():
                    raise DeploymentError("目标仓库路径不能为空。")
                assert_plain_chain(target_root_input)
                target_root = canonical(os.path.realpath(native(target_root_input)))
                root_state = _entry_state(target_root)
                if root_state is None or root_state.kind != "dir":
                    raise DeploymentError(f"目标仓库不存在或不是普通目录：{target_root}")
                if Path(target_root).parent == Path(target_root):
                    raise DeploymentError("目标仓库不能是磁盘根目录。")
                desired_target = canonical(os.path.join(target_root, *destination_parts))
                existing, parent_guards = _resolve_relative_existing(target_root, destination_parts)
                if existing:
                    existing_state = _entry_state(existing)
                    if existing_state is None or existing_state.kind != "dir":
                        raise DeploymentError(f"限定目标已存在但不是普通目录：{existing}")
                pair_key = (selection.component.component_id.casefold(), selection.target.target_id.casefold())
                if pair_key in target_ids:
                    raise DeploymentError("同一组件和目标仓库被重复选择。")
                target_ids.add(pair_key)
                resolved.append((
                    selection, source_path, source_authorization_root,
                    target_root, existing, parent_guards,
                ))
                source_paths.append(source_path)
                source_authorization_roots.append(source_authorization_root)
                target_roots.append(target_root)
                logical_targets.append(desired_target)
            self._check_state_location(
                (*source_paths, *source_authorization_roots), target_roots
            )
            for index, target_path in enumerate(logical_targets):
                for source_root in (*source_paths, *source_authorization_roots):
                    if (_same_or_contains(source_root, target_path)
                            or _same_or_contains(target_path, source_root)):
                        raise DeploymentError("组件来源与任一限定目标不能相同或互相包含。")
                for other in logical_targets[index + 1:]:
                    if _same_or_contains(target_path, other) or _same_or_contains(other, target_path):
                        raise DeploymentError("多个限定目标不能相同或互相包含。")
            source_cache: dict[str, FrozenTree] = {}
            target_plans: list[DeploymentTargetPlan] = []
            for index, (
                    selection, source_path, source_authorization_root,
                    target_root, existing, parent_guards) in enumerate(resolved):
                _cancelled(cancel)
                source_key = _case_key(source_path)
                if source_key not in source_cache:
                    source_cache[source_key] = _scan_tree(
                        source_path, cancel, progress,
                        progress_values={"batch_id": batch_id,
                                         "component_id": selection.component.component_id,
                                         "target_id": selection.target.target_id},
                    )
                source_tree = source_cache[source_key]
                target_tree = (_scan_tree(existing, cancel, progress,
                                          progress_values={"batch_id": batch_id,
                                                           "component_id": selection.component.component_id,
                                                           "target_id": selection.target.target_id})
                               if existing else None)
                target_root_state = _entry_state(target_root)
                if target_root_state is None or target_root_state.kind != "dir":
                    raise DeploymentError(f"目标仓库在分析期间发生变化：{target_root}")
                selection_id = str(uuid.uuid5(uuid.UUID(batch_id), f"{index}:{selection.component.component_id}:"
                                                                   f"{selection.target.target_id}"))
                desired_target = logical_targets[index]
                changes = _compare_entries(source_tree, target_tree)
                target_plans.append(DeploymentTargetPlan(
                    selection_id=selection_id,
                    component_id=selection.component.component_id,
                    target_id=selection.target.target_id,
                    source_path=source_path,
                    source_authorization_root=source_authorization_root,
                    target_root=target_root,
                    target_path=desired_target,
                    existing_target_path=existing or None,
                    changes=changes,
                    needs_deploy=not _portable_equal(source_tree, target_tree),
                    source_tree=source_tree,
                    target_tree=target_tree,
                    target_volume=volume_identity(target_root),
                    target_root_snapshot=target_root_state,
                    parent_guards=parent_guards,
                ))
                _emit(progress, "analyzed-target", batch_id=batch_id,
                      selection_id=selection_id, component_id=selection.component.component_id,
                      target_id=selection.target.target_id, message="目标文件与目录预览已完成")
            created_at = time.time()
            targets = tuple(target_plans)
            return DeploymentPlan(batch_id, request.label, targets, created_at,
                                  _seal_plan(batch_id, request.label, targets, created_at))
        finally:
            self._lock.release()

    def _validate_plan(self, plan: DeploymentPlan) -> None:
        try:
            if str(uuid.UUID(plan.batch_id)) != plan.batch_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise DeploymentError("部署计划批次编号无效。") from exc
        if not plan.targets:
            raise DeploymentError("部署计划没有目标。")
        if plan.seal != _seal_plan(plan.batch_id, plan.label, plan.targets, plan.created_at):
            raise DeploymentError("部署计划已被修改，请重新分析。")
        self._check_state_location(
            (root for item in plan.targets
             for root in (item.source_path, item.source_authorization_root)),
            (item.target_root for item in plan.targets),
        )

    def _revalidate_source(self, tree: FrozenTree, cancel: threading.Event | None) -> None:
        if volume_identity(tree.root) != tree.volume:
            raise DeploymentError(f"来源磁盘身份已改变：{tree.root}")
        current = _scan_tree(tree.root, cancel, None)
        if not _tree_matches(current, tree):
            raise DeploymentError(f"组件来源在预览后发生变化：{tree.root}")

    def _current_target_path(self, item: DeploymentTargetPlan,
                             created: dict[str, OwnedDirectory] | None = None) -> str | None:
        parts = tuple(os.path.relpath(item.target_path, item.target_root).replace("\\", "/").split("/"))
        current = item.target_root
        for index, part in enumerate(parts):
            found = _find_case_child(current, part)
            if found is None:
                return None
            if index < len(parts) - 1:
                state = _entry_state(found)
                if state is None or state.kind != "dir":
                    raise DeploymentError(f"目标父路径已变成非目录：{found}")
                guard = item.parent_guards[index]
                key = _case_key(guard.desired_path)
                owned = (created or {}).get(key)
                expected = owned.identity if owned else _identity_tuple(guard.snapshot)
                if identity(snapshot(found)) != expected:
                    raise DeploymentError(f"目标父目录身份已改变：{found}")
            current = found
        return current

    def _revalidate_target(self, item: DeploymentTargetPlan,
                           created: dict[str, OwnedDirectory] | None = None) -> None:
        assert_plain_chain(item.target_root)
        if volume_identity(item.target_root) != item.target_volume:
            raise DeploymentError(f"目标磁盘身份已改变：{item.target_root}")
        if identity(snapshot(item.target_root)) != item.target_root_snapshot.identity:
            raise DeploymentError(f"目标仓库目录已被替换：{item.target_root}")
        current_path = self._current_target_path(item, created)
        if item.target_tree is None:
            if current_path is not None:
                raise DeploymentError(f"目标子树在预览后已出现：{current_path}")
        else:
            if current_path is None:
                raise DeploymentError(f"目标子树在预览后消失：{item.target_path}")
            current = _scan_tree(current_path, None, None)
            if not _tree_matches(current, replace(item.target_tree, root=current_path)):
                raise DeploymentError(f"目标子树在预览后发生变化：{current_path}")

    def _stage_name(self, plan: DeploymentPlan, item: DeploymentTargetPlan, suffix: str) -> str:
        short_batch = plan.batch_id.replace("-", "")
        short_selection = item.selection_id.replace("-", "")
        return canonical(os.path.join(
            item.target_root, f"{_OWNED_PREFIX}{short_batch}-{short_selection}.{suffix}"
        ))

    def _copy_to_stage(self, plan: DeploymentPlan, item: DeploymentTargetPlan, stage: str,
                       cancel: threading.Event | None, progress: ProgressCallback) -> None:
        directories = [entry for entry in item.source_tree.entries if entry.snapshot.kind == "dir"]
        files = [entry for entry in item.source_tree.entries if entry.snapshot.kind == "file"]
        for entry in sorted(directories, key=lambda value: (value.relative_path.count("/"),
                                                             _case_key(value.relative_path))):
            _cancelled(cancel)
            destination = checked_child(stage, entry.relative_path)
            if snapshot(destination) is not None:
                raise DeploymentError(f"暂存目录出现意外项目：{destination}")
            os.mkdir(native(destination))
        completed_bytes = 0
        for number, entry in enumerate(files, 1):
            _cancelled(cancel)
            source = checked_child(item.source_path, entry.relative_path)
            destination = checked_child(stage, entry.relative_path)
            if not _entry_matches(_entry_state(source), entry.snapshot):
                raise DeploymentError(f"暂存前来源文件已改变：{entry.relative_path}")
            digest = hashlib.sha256()
            copied = 0
            try:
                with open(native(source), "rb") as input_stream:
                    opened = os.fstat(input_stream.fileno())
                    expected_tuple = (entry.snapshot.size, entry.snapshot.mtime_ns,
                                      entry.snapshot.inode, entry.snapshot.device)
                    if (opened.st_size, opened.st_mtime_ns,
                            opened.st_ino, opened.st_dev) != expected_tuple:
                        raise DeploymentError(f"打开来源文件时内容身份已改变：{entry.relative_path}")
                    with open(native(destination), "xb") as output_stream:
                        while True:
                            _cancelled(cancel)
                            block = input_stream.read(CHUNK_SIZE)
                            if not block:
                                break
                            output_stream.write(block)
                            digest.update(block)
                            copied += len(block)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                    finished = os.fstat(input_stream.fileno())
                    if (finished.st_size, finished.st_mtime_ns,
                            finished.st_ino, finished.st_dev) != expected_tuple:
                        raise DeploymentError(f"复制期间来源文件已改变：{entry.relative_path}")
            except OSError as exc:
                raise DeploymentError(f"暂存文件失败：{entry.relative_path}（{exc}）") from exc
            if copied != entry.snapshot.size or digest.hexdigest() != entry.snapshot.sha256:
                raise DeploymentError(f"暂存期间来源内容已改变：{entry.relative_path}")
            staged_state = _entry_state(destination)
            if staged_state is None or staged_state.kind != "file" or staged_state.size != copied:
                raise DeploymentError(f"暂存文件不完整：{entry.relative_path}")
            if _hash_file(destination, staged_state, cancel) != entry.snapshot.sha256:
                raise DeploymentError(f"暂存文件 SHA-256 校验失败：{entry.relative_path}")
            # The path was just created by this transaction and revalidated as
            # a regular file.  Windows does not implement follow_symlinks=False
            # for os.utime, so use the already-checked extended path directly.
            os.utime(native(destination), ns=(entry.snapshot.mtime_ns, entry.snapshot.mtime_ns))
            if not _entry_matches(_entry_state(source), entry.snapshot):
                raise DeploymentError(f"暂存后来源文件已改变：{entry.relative_path}")
            completed_bytes += copied
            _emit(progress, "stage", batch_id=plan.batch_id,
                  selection_id=item.selection_id, component_id=item.component_id,
                  target_id=item.target_id, relative_path=entry.relative_path,
                  completed_files=number, total_files=len(files),
                  completed_bytes=completed_bytes, total_bytes=item.source_tree.total_bytes,
                  message="已暂存并完成 SHA-256 校验")
        manifest, _ = _manifest_tree(stage, cancel)
        if manifest != item.source_tree.manifest():
            raise DeploymentError(f"目标卷暂存目录校验不一致：{item.target_id}")

    def _cleanup_stage(self, batch_id: str, record: JournalTarget) -> None:
        if not record.stage_path or snapshot(record.stage_path) is None:
            return
        self._assert_owned_path(batch_id, record, record.stage_path, "stage")
        _remove_owned_tree(record.stage_path, record.deployed_manifest,
                           record.stage_identity, "暂存目录", allow_subset=True,
                           allow_incomplete_files=True)

    def _prepare(self, plan: DeploymentPlan, cancel: threading.Event | None,
                 progress: ProgressCallback) -> int:
        changed = [item for item in plan.targets if item.needs_deploy]
        bytes_by_volume: dict[str, int] = {}
        root_by_volume: dict[str, str] = {}
        for item in changed:
            bytes_by_volume[item.target_volume] = bytes_by_volume.get(item.target_volume, 0) + item.source_tree.total_bytes
            root_by_volume[item.target_volume] = item.target_root
        for volume, required in bytes_by_volume.items():
            free = shutil.disk_usage(native(root_by_volume[volume])).free
            if free < required:
                raise DeploymentError(f"目标磁盘暂存空间不足：需要 {required:,} 字节，可用 {free:,} 字节。")
        prepared = 0
        for item in changed:
            _cancelled(cancel)
            self._revalidate_source(item.source_tree, cancel)
            self._revalidate_target(item)
            stage = self._stage_name(plan, item, "stage")
            if snapshot(stage) is not None:
                raise DeploymentError(f"事务暂存路径已存在：{stage}")
            os.mkdir(native(stage))
            stage_state = _entry_state(stage)
            if stage_state is None or stage_state.kind != "dir":
                raise DeploymentError(f"无法建立事务暂存目录：{stage}")
            if volume_identity(stage) != item.target_volume:
                raise DeploymentError(f"事务暂存目录不在目标磁盘上：{stage}")
            try:
                self.journal.set_target(plan.batch_id, item.selection_id,
                                        phase="staging", stage_path=stage,
                                        stage_identity=stage_state.identity)
            except JournalError:
                # The path is not recoverable until path and identity are
                # durably registered together.  This live process may clean
                # its known-empty directory; a crash instead leaves it inert.
                _remove_owned_tree(stage, {"": {"kind": "dir"}},
                                   stage_state.identity, "未登记的暂存目录")
                raise
            self._copy_to_stage(plan, item, stage, cancel, progress)
            self._revalidate_source(item.source_tree, cancel)
            self._revalidate_target(item)
            self.journal.set_target(plan.batch_id, item.selection_id, phase="prepared")
            prepared += 1
        # A single final barrier ensures every source and target still matches
        # the one preview shared by all staged copies before any commit starts.
        seen_sources: set[str] = set()
        for item in plan.targets:
            source_key = _case_key(item.source_path)
            if source_key not in seen_sources:
                self._revalidate_source(item.source_tree, cancel)
                seen_sources.add(source_key)
            self._revalidate_target(item)
        return prepared

    def _append_created_parent(self, batch_id: str, selection_id: str,
                               owned: OwnedDirectory) -> None:
        current = self.journal.get(batch_id)
        target = next(item for item in current.targets if item.selection_id == selection_id)
        values = tuple(item for item in target.created_parents if _case_key(item.path) != _case_key(owned.path))
        self.journal.set_target(batch_id, selection_id, created_parents=values + (owned,))

    def _ensure_parents(self, plan: DeploymentPlan, item: DeploymentTargetPlan,
                        created: dict[str, OwnedDirectory]) -> None:
        for guard in item.parent_guards:
            key = _case_key(guard.desired_path)
            if guard.snapshot is not None:
                _assert_identity(guard.actual_path or guard.desired_path,
                                 guard.snapshot.identity, "目标父目录")
                continue
            owned = created.get(key)
            if owned is not None:
                _assert_identity(owned.path, owned.identity, "本批次创建的父目录")
                continue
            parent = canonical(os.path.dirname(guard.desired_path))
            name = os.path.basename(guard.desired_path)
            if _find_case_child(parent, name) is not None:
                raise DeploymentError(f"目标父目录在预览后已出现：{guard.desired_path}")
            os.mkdir(native(guard.desired_path))
            state = _entry_state(guard.desired_path)
            if state is None or state.kind != "dir":
                raise DeploymentError(f"无法创建目标父目录：{guard.desired_path}")
            owned = OwnedDirectory(guard.desired_path, state.identity)
            created[key] = owned
            try:
                self._append_created_parent(plan.batch_id, item.selection_id, owned)
            except JournalError:
                # Never authorize an identity-less parent.  Remove only this
                # live process's still-empty, identity-checked directory.
                _rmdir_owned(owned.path, owned.identity)
                created.pop(key, None)
                raise

    def _commit_one(self, plan: DeploymentPlan, item: DeploymentTargetPlan,
                    created: dict[str, OwnedDirectory], progress: ProgressCallback) -> None:
        self._revalidate_source(item.source_tree, None)
        self._revalidate_target(item, created)
        record = next(target for target in self.journal.get(plan.batch_id).targets
                      if target.selection_id == item.selection_id)
        if not record.stage_path or record.stage_identity is None:
            raise DeploymentError("事务暂存记录不完整。")
        self._assert_owned_path(plan.batch_id, record, record.stage_path, "stage")
        _assert_manifest(record.stage_path, record.deployed_manifest,
                         record.stage_identity, "暂存目录")
        self._ensure_parents(plan, item, created)
        current = self._current_target_path(item, created)
        backup: str | None = None
        if item.target_tree is not None:
            if current is None:
                raise DeploymentError(f"提交前目标子树已消失：{item.target_path}")
            target_manifest, target_identity = _manifest_tree(current)
            if (target_manifest != item.target_tree.manifest()
                    or target_identity != item.target_tree.root_snapshot.identity):
                raise DeploymentError(f"提交前目标子树已改变：{current}")
            backup = self._stage_name(plan, item, "backup")
            if snapshot(backup) is not None:
                raise DeploymentError(f"事务备份路径已存在：{backup}")
            self.journal.set_target(plan.batch_id, item.selection_id,
                                    phase="backing_up", backup_path=backup,
                                    backup_identity=item.target_tree.root_snapshot.identity)
            _rename_directory(current, backup)
            backup_manifest, backup_identity = _manifest_tree(backup)
            if (backup_manifest != item.target_tree.manifest()
                    or backup_identity != item.target_tree.root_snapshot.identity):
                raise DeploymentError(f"目标备份切换后校验失败：{backup}")
            self.journal.set_target(plan.batch_id, item.selection_id,
                                    phase="backed_up", backup_identity=backup_identity)
        else:
            if current is not None:
                raise DeploymentError(f"提交前目标子树已出现：{current}")
        self.journal.set_target(plan.batch_id, item.selection_id, phase="installing")
        _rename_directory(record.stage_path, item.target_path)
        deployed_manifest, deployed_identity = _manifest_tree(item.target_path)
        if (deployed_manifest != item.source_tree.manifest()
                or deployed_identity != record.stage_identity):
            raise DeploymentError(f"新部署目录切换后校验失败：{item.target_path}")
        self.journal.set_target(plan.batch_id, item.selection_id,
                                phase="committed", deployed_identity=record.stage_identity)
        _emit(progress, "committed", batch_id=plan.batch_id,
              selection_id=item.selection_id, component_id=item.component_id,
              target_id=item.target_id, message="限定子树已原子切换，旧版本仍保留为事务备份")

    def execute(self, plan: DeploymentPlan, cancel: threading.Event | None = None,
                progress: ProgressCallback = None) -> DeploymentResult:
        started = time.monotonic()
        if not self._lock.acquire(blocking=False):
            return DeploymentResult(plan.batch_id, "failed", errors=("已有仓库管理任务正在运行。",))
        prepared = committed = rolled_back = 0
        status = "failed"
        errors: list[str] = []
        record_created = False
        process_locks: _InterprocessLockSet | None = None
        try:
            _cancelled(cancel)
            self._validate_plan(plan)
            process_locks = _InterprocessLockSet(
                self.state_dir, (item.target_root for item in plan.targets),
                (root for item in plan.targets
                 for root in (item.source_path, item.source_authorization_root)),
            )
            process_locks.acquire()
            for item in plan.targets:
                self._revalidate_source(item.source_tree, cancel)
                self._revalidate_target(item)
            # Establish/repair a key only under the strict journal rule that no
            # existing authority may depend on invalid bytes, then use the
            # authenticated history as the new-operation gate.
            self.journal.prepare_new_batch()
            pending = tuple(
                batch for batch in self.journal.list()
                if batch.status not in _TERMINAL_BATCH_STATUSES
            )
            if pending:
                identifiers = "、".join(batch.batch_id[:8] for batch in pending[:3])
                raise DeploymentError(
                    f"已有待恢复部署批次（{identifiers}），请先撤销或确认保留。"
                )
            record = JournalBatch(
                batch_id=plan.batch_id, label=plan.label, status="preparing",
                targets=tuple(_journal_target(item) for item in plan.targets),
                created_at=plan.created_at, updated_at=time.time(),
            )
            self.journal.create(record)
            record_created = True
            prepared = self._prepare(plan, cancel, progress)
            self.journal.set_batch(plan.batch_id, status="prepared")
            _cancelled(cancel)
            self.journal.set_batch(plan.batch_id, status="committing")
            created: dict[str, OwnedDirectory] = {}
            for item in plan.targets:
                if not item.needs_deploy:
                    continue
                _cancelled(cancel)
                self._commit_one(plan, item, created, progress)
                committed += 1
            self.journal.set_batch(plan.batch_id, status="committed")
            status = "success"
            _emit(progress, "done", batch_id=plan.batch_id,
                  completed_files=committed, total_files=prepared,
                  message="全部限定子树已部署；可确认保留或撤销本批次")
        except SyncCancelled as exc:
            status = "cancelled"
            errors.append(str(exc))
            if record_created:
                rolled_back, recovery_errors = self._recover_failed_execution(
                    plan.batch_id, str(exc), cancelled=True
                )
                errors.extend(recovery_errors)
        except (OSError, ValueError, SyncError) as exc:
            errors.append(str(exc))
            if record_created:
                rolled_back, recovery_errors = self._recover_failed_execution(
                    plan.batch_id, str(exc), cancelled=False
                )
                errors.extend(recovery_errors)
        finally:
            journal_status = ""
            if record_created:
                try:
                    journal_status = self.journal.get(plan.batch_id).status
                except JournalError as exc:
                    errors.append(str(exc))
            if process_locks is not None:
                process_locks.release()
            self._lock.release()
        return DeploymentResult(
            batch_id=plan.batch_id, status=status, journal_status=journal_status,
            prepared_targets=prepared, committed_targets=committed,
            rolled_back_targets=rolled_back, errors=tuple(errors),
            duration_seconds=time.monotonic() - started,
        )

    def _recover_failed_execution(self, batch_id: str, error: str,
                                  *, cancelled: bool) -> tuple[int, list[str]]:
        recovery_errors: list[str] = []
        try:
            record = self.journal.get(batch_id)
            commit_started = record.status in ("committing", "commit_failed") or any(
                target.phase in ("backing_up", "backed_up", "installing", "committed")
                for target in record.targets
            )
            if commit_started:
                # Persist a recovery-required state before attempting rollback.
                # A crash between this write and rollback must never make a
                # partially switched transaction look terminal.
                self.journal.set_batch(
                    batch_id, status="rollback_required", error=error
                )
                result = self._rollback_locked(batch_id, automatic=True)
                recovery_errors.extend(result.errors)
                return result.rolled_back_targets, recovery_errors
            record = self.journal.set_batch(
                batch_id, status="recovery_required", error=error
            )
            cleaned = 0
            for target in reversed(record.targets):
                if target.stage_path and snapshot(target.stage_path) is not None:
                    self._cleanup_stage(batch_id, target)
                    cleaned += 1
                self.journal.set_target(batch_id, target.selection_id, phase="cancelled" if cancelled else "prepare_failed")
            self.journal.set_batch(
                batch_id, status="cancelled" if cancelled else "prepare_failed"
            )
            return 0, recovery_errors
        except (OSError, SyncError) as exc:
            recovery_errors.append(f"事务恢复未完成：{exc}")
            try:
                self.journal.set_batch(batch_id, status="recovery_required", error=str(exc))
            except JournalError:
                pass
            return 0, recovery_errors

    def get_batch(self, batch_id: str) -> JournalBatch:
        return self.journal.get(batch_id)

    def list_batches(self) -> tuple[JournalBatch, ...]:
        return self.journal.list()

    def _assert_owned_path(self, batch_id: str, target: JournalTarget,
                           path: str, suffix: str) -> None:
        canonical_path = canonical(path)
        try:
            relative = os.path.relpath(canonical_path, target.target_root).replace("\\", "/")
        except ValueError as exc:
            raise DeploymentError("事务日志中的暂存或备份路径越出目标仓库。") from exc
        parts = tuple(relative.split("/"))
        if (len(parts) != 1 or parts[0] in ("", ".", "..") or ":" in parts[0]
                or _path_key(os.path.dirname(canonical_path)) != _path_key(target.target_root)):
            raise DeploymentError("事务日志中的暂存或备份路径越出目标仓库。")
        expected_name = (
            f"{_OWNED_PREFIX}{batch_id.replace('-', '')}-"
            f"{target.selection_id.replace('-', '')}.{suffix}"
        )
        expected_path = canonical(os.path.join(target.target_root, expected_name))
        if _path_key(canonical_path) != _path_key(expected_path):
            raise DeploymentError("事务日志中的暂存或备份路径名称无效。")

    def _validate_record(self, batch: JournalBatch) -> None:
        sources = tuple(
            root for target in batch.targets
            for root in (target.source_path, target.source_authorization_root)
        )
        target_roots = tuple(target.target_root for target in batch.targets)
        target_paths = tuple(target.target_path for target in batch.targets)
        self._check_state_location(sources, target_roots)
        for index, target_path in enumerate(target_paths):
            for source_path in sources:
                if (_same_or_contains(source_path, target_path)
                        or _same_or_contains(target_path, source_path)):
                    raise DeploymentError("事务授权中的来源与目标相同或互相包含。")
            for other in target_paths[index + 1:]:
                if (_same_or_contains(target_path, other) or _same_or_contains(other, target_path)):
                    raise DeploymentError("事务授权中的多个目标相同或互相包含。")
        for target in batch.targets:
            try:
                if str(uuid.UUID(target.selection_id)) != target.selection_id:
                    raise ValueError
            except (ValueError, AttributeError) as exc:
                raise DeploymentError("事务日志中的目标编号无效。") from exc
            target_parts = _relative_parts(target.target_relative, field_name="日志目标子目录")
            rebuilt_target = canonical(os.path.join(target.target_root, *target_parts))
            if (_path_key(rebuilt_target) != _path_key(target.target_path)
                    or _path_key(target.target_path) == _path_key(target.target_root)
                    or not _same_or_contains(target.target_root, target.target_path)):
                raise DeploymentError("事务日志中的限定目标越出目标仓库。")
            if Path(target.target_root).parent == Path(target.target_root):
                raise DeploymentError("事务日志中的目标仓库不能是磁盘根目录。")
            allowed_parents = {
                _path_key(canonical(os.path.join(target.target_root, *target_parts[:index])))
                for index in range(1, len(target_parts))
            }
            previous_depth = 0
            seen_parents: set[str] = set()
            for parent in target.created_parents:
                parent_key = _path_key(parent.path)
                try:
                    relative_parent = os.path.relpath(parent.path, target.target_root).replace("\\", "/")
                except ValueError as exc:
                    raise DeploymentError("事务日志中的父目录路径越出目标仓库。") from exc
                parent_parts = tuple(relative_parent.split("/"))
                if (parent_key not in allowed_parents or parent_key in seen_parents
                        or any(part in ("", ".", "..") or ":" in part for part in parent_parts)
                        or len(parent_parts) <= previous_depth
                        or (parent.identity is not None and parent.identity[0] != "dir")):
                    raise DeploymentError("事务日志中的父目录与限定目标不一致。")
                seen_parents.add(parent_key)
                previous_depth = len(parent_parts)
            assert_plain_chain(target.target_root)
            if volume_identity(target.target_root) != target.target_volume:
                raise DeploymentError(f"目标磁盘身份已改变：{target.target_root}")
            _assert_identity(target.target_root, target.target_root_identity, "目标仓库")
            for path, suffix in ((target.stage_path, "stage"),
                                 (target.backup_path, "backup"),
                                 (target.rollback_path, "rollback")):
                if path:
                    self._assert_owned_path(batch.batch_id, target, path, suffix)

    def _classify_current(self, target: JournalTarget) -> tuple[str, str | None]:
        parts = _relative_parts(target.target_relative, field_name="日志目标子目录")
        current = target.target_root
        for index, part in enumerate(parts):
            found = _find_case_child(current, part)
            if found is None:
                return "missing", None
            if index < len(parts) - 1:
                state = _entry_state(found)
                if state is None or state.kind != "dir":
                    raise DeploymentError(f"事务日志中的目标父级不是普通目录：{found}")
            current = found
        manifest, root_identity = _manifest_tree(current)
        if (target.original_manifest is not None and manifest == target.original_manifest
                and (target.original_identity is None or root_identity == target.original_identity)):
            return "original", current
        expected_deployed_identity = target.deployed_identity or target.stage_identity
        if (manifest == target.deployed_manifest and
                (expected_deployed_identity is None or root_identity == expected_deployed_identity)):
            return "deployed", current
        raise DeploymentError(f"当前部署目录不再匹配事务快照：{current}")

    def _preflight_rollback(self, batch: JournalBatch) -> dict[str, tuple[str, str | None]]:
        self._validate_record(batch)
        states: dict[str, tuple[str, str | None]] = {}
        for target in batch.targets:
            if target.phase in ("unchanged", "rolled_back"):
                states[target.selection_id] = ("original" if target.original_manifest else "missing", None)
                continue
            if target.stage_path and snapshot(target.stage_path) is not None:
                self._assert_owned_path(batch.batch_id, target, target.stage_path, "stage")
                _assert_manifest(target.stage_path, target.deployed_manifest,
                                 target.stage_identity, "事务暂存目录", allow_subset=True,
                                 allow_incomplete_files=True)
            backup_exists = bool(target.backup_path and snapshot(target.backup_path) is not None)
            if backup_exists:
                self._assert_owned_path(batch.batch_id, target, target.backup_path or "", "backup")
                _assert_manifest(target.backup_path or "", target.original_manifest or {},
                                 target.backup_identity or target.original_identity,
                                 "事务备份目录")
            rollback_exists = bool(target.rollback_path and snapshot(target.rollback_path) is not None)
            if rollback_exists:
                self._assert_owned_path(batch.batch_id, target, target.rollback_path or "", "rollback")
                _assert_manifest(target.rollback_path or "", target.deployed_manifest,
                                 target.rollback_identity or target.deployed_identity,
                                 "回滚隔离目录", allow_subset=True)
            state, current = self._classify_current(target)
            if target.original_manifest is not None:
                if backup_exists and state not in ("deployed", "missing", "original"):
                    raise DeploymentError(f"无法判断目标恢复状态：{target.target_path}")
                if not backup_exists and state != "original":
                    raise DeploymentError(f"原目标备份不可用，已拒绝删除当前部署：{target.target_path}")
            elif backup_exists:
                raise DeploymentError(f"新增目标不应存在旧目录备份：{target.target_path}")
            elif state not in ("deployed", "missing"):
                raise DeploymentError(f"新增目标已被其他内容替换：{target.target_path}")
            states[target.selection_id] = (state, current)
        return states

    def _remove_created_parents(self, target: JournalTarget) -> tuple[str, ...]:
        residuals: list[str] = []
        for owned in reversed(target.created_parents):
            state = snapshot(owned.path)
            if state is None:
                continue
            if identity(state) != owned.identity or state["kind"] != "dir":
                residuals.append(f"本批次创建的父目录已改变，已保留：{owned.path}")
                continue
            try:
                _rmdir_owned(owned.path, owned.identity)
            except (OSError, SyncError) as exc:
                # Parent cleanup is best-effort after the bounded target has
                # already been restored.  An unregistered child can originate
                # from a crash between mkdir and journal registration; never
                # recurse into it and never keep recovery globally blocked.
                residuals.append(
                    f"本批次创建的父目录不为空或无法证明可删，已保留："
                    f"{owned.path}（{exc}）"
                )
        return tuple(residuals)

    def _rollback_locked(self, batch_id: str, *, automatic: bool = False) -> DeploymentResult:
        started = time.monotonic()
        batch = self.journal.get(batch_id)
        if batch.status in ("rolled_back", "rolled_back_with_residuals"):
            return DeploymentResult(batch_id, "success", batch.status, rolled_back_targets=0,
                                    errors=batch.errors if batch.status.endswith("residuals") else ())
        if batch.status == "finalized":
            return DeploymentResult(batch_id, "failed", "finalized",
                                    errors=("该批次已确认保留，事务备份已经清理。",))
        try:
            states = self._preflight_rollback(batch)
        except (OSError, SyncError) as exc:
            try:
                self.journal.set_batch(batch_id, status="rollback_blocked", error=str(exc))
            except JournalError:
                pass
            return DeploymentResult(batch_id, "failed", "rollback_blocked", errors=(str(exc),),
                                    duration_seconds=time.monotonic() - started)
        self.journal.set_batch(batch_id, status="rolling_back")
        restored = 0
        errors: list[str] = []
        try:
            for target in reversed(batch.targets):
                state, current = states[target.selection_id]
                if target.phase in ("unchanged", "rolled_back"):
                    self.journal.set_target(batch_id, target.selection_id, phase="rolled_back")
                    continue
                backup_exists = bool(target.backup_path and snapshot(target.backup_path) is not None)
                rollback_path = target.rollback_path or canonical(os.path.join(
                    target.target_root,
                    f"{_OWNED_PREFIX}{batch.batch_id.replace('-', '')}-"
                    f"{target.selection_id.replace('-', '')}.rollback",
                ))
                if snapshot(rollback_path) is not None:
                    self._assert_owned_path(batch.batch_id, target, rollback_path, "rollback")
                moved_current = snapshot(rollback_path) is not None
                authorized_rollback_identity = (
                    target.rollback_identity
                    or target.deployed_identity
                    or target.stage_identity
                )
                if state == "deployed" and current is not None:
                    if moved_current:
                        raise DeploymentError(f"回滚隔离路径已存在：{rollback_path}")
                    if authorized_rollback_identity is None:
                        raise DeploymentError("回滚隔离目录缺少已认证身份。")
                    self.journal.set_target(batch_id, target.selection_id,
                                            phase="rollback_moving_deployed",
                                            rollback_path=rollback_path,
                                            rollback_identity=authorized_rollback_identity)
                    _rename_directory(current, rollback_path)
                    moved_current = True
                    _assert_identity(
                        rollback_path, authorized_rollback_identity, "回滚隔离目录"
                    )
                if target.original_manifest is not None and backup_exists:
                    try:
                        _rename_directory(target.backup_path or "", target.target_path)
                    except OSError:
                        if moved_current:
                            _rename_directory(rollback_path, target.target_path)
                        raise
                    _assert_manifest(target.target_path, target.original_manifest,
                                     target.original_identity, "已恢复的原目标")
                if moved_current and snapshot(rollback_path) is not None:
                    if authorized_rollback_identity is None:
                        raise DeploymentError("回滚隔离目录缺少已认证身份。")
                    _remove_owned_tree(rollback_path, target.deployed_manifest,
                                       authorized_rollback_identity, "回滚隔离目录",
                                       allow_subset=True)
                if target.stage_path and snapshot(target.stage_path) is not None:
                    self._cleanup_stage(batch.batch_id, target)
                target_residuals = self._remove_created_parents(target)
                self.journal.set_target(batch_id, target.selection_id,
                                        phase="rolled_back", rollback_path=None,
                                        rollback_identity=None,
                                        residuals=target.residuals + target_residuals)
                restored += 1
            completed = self.journal.get(batch_id)
            residuals = tuple(
                warning for target in completed.targets for warning in target.residuals
            )
            terminal_status = (
                "rolled_back_with_residuals" if residuals else "rolled_back"
            )
            self.journal.set_batch(
                batch_id, status=terminal_status,
                error="\n".join(residuals) if residuals else None,
            )
            return DeploymentResult(batch_id, "success", terminal_status,
                                    rolled_back_targets=restored,
                                    errors=tuple(residuals),
                                    duration_seconds=time.monotonic() - started)
        except (OSError, SyncError) as exc:
            errors.append(f"回滚未完成：{exc}")
            try:
                self.journal.set_batch(batch_id, status="rollback_required", error=str(exc))
            except JournalError:
                pass
            return DeploymentResult(batch_id, "failed", "rollback_required",
                                    rolled_back_targets=restored, errors=tuple(errors),
                                    duration_seconds=time.monotonic() - started)

    def rollback(self, batch_id: str) -> DeploymentResult:
        if not self._lock.acquire(blocking=False):
            return DeploymentResult(batch_id, "failed", errors=("已有仓库管理任务正在运行。",))
        process_locks: _InterprocessLockSet | None = None
        try:
            try:
                batch = self.journal.get(batch_id)
                process_locks = _InterprocessLockSet(
                    self.state_dir, (target.target_root for target in batch.targets),
                    (root for target in batch.targets
                     for root in (target.source_path, target.source_authorization_root)),
                )
                process_locks.acquire()
                return self._rollback_locked(batch_id)
            except (OSError, ValueError, SyncError) as exc:
                return DeploymentResult(batch_id, "failed", errors=(str(exc),))
        finally:
            if process_locks is not None:
                process_locks.release()
            self._lock.release()

    def finalize(self, batch_id: str) -> DeploymentResult:
        started = time.monotonic()
        if not self._lock.acquire(blocking=False):
            return DeploymentResult(batch_id, "failed", errors=("已有仓库管理任务正在运行。",))
        cleaned = 0
        process_locks: _InterprocessLockSet | None = None
        try:
            batch = self.journal.get(batch_id)
            process_locks = _InterprocessLockSet(
                self.state_dir, (target.target_root for target in batch.targets),
                (root for target in batch.targets
                 for root in (target.source_path, target.source_authorization_root)),
            )
            process_locks.acquire()
            batch = self.journal.get(batch_id)
            if batch.status == "finalized":
                return DeploymentResult(batch_id, "success", "finalized")
            if batch.status not in ("committed", "finalizing", "finalize_required"):
                raise DeploymentError("只有完整提交的批次才能确认保留。")
            self._validate_record(batch)
            # A backup may only be discarded while its live target is still
            # the authenticated deployment installed by this batch.  Keep the
            # same requirement for retries from ``finalizing``: partial backup
            # cleanup does not authorize finalizing a missing/replaced target.
            for target in batch.targets:
                if target.phase == "finalized":
                    continue
                if target.phase not in ("unchanged", "committed", "finalizing"):
                    raise DeploymentError(
                        f"目标尚未完整提交，无法确认保留：{target.target_path}"
                    )
                current_state, _ = self._classify_current(target)
                expected_state = "original" if target.phase == "unchanged" else "deployed"
                if current_state != expected_state:
                    raise DeploymentError(
                        "当前目标不是本批次已认证的部署快照，拒绝清理备份："
                        f"{target.target_path}"
                    )
            # Validate every cleanup candidate before removing the first one.
            for target in batch.targets:
                if target.phase == "finalized":
                    continue
                if target.backup_path:
                    if snapshot(target.backup_path) is None:
                        if target.phase == "finalizing" and (
                                not target.stage_path or snapshot(target.stage_path) is None):
                            continue
                        raise DeploymentError(f"事务备份已丢失，无法安全确认：{target.backup_path}")
                    self._assert_owned_path(batch.batch_id, target, target.backup_path, "backup")
                    _assert_manifest(target.backup_path, target.original_manifest or {},
                                     target.backup_identity or target.original_identity,
                                     "事务备份目录",
                                     allow_subset=target.phase == "finalizing")
                if target.stage_path and snapshot(target.stage_path) is not None:
                    self._assert_owned_path(batch.batch_id, target, target.stage_path, "stage")
                    _assert_manifest(target.stage_path, target.deployed_manifest,
                                     target.stage_identity, "事务暂存目录",
                                     allow_subset=target.phase == "finalizing")
            self.journal.set_batch(batch_id, status="finalizing")
            for target in batch.targets:
                if target.phase == "finalized":
                    continue
                self.journal.set_target(batch_id, target.selection_id, phase="finalizing")
                if target.backup_path and snapshot(target.backup_path) is not None:
                    _remove_owned_tree(target.backup_path, target.original_manifest or {},
                                       target.backup_identity or target.original_identity,
                                       "事务备份目录", allow_subset=True)
                    cleaned += 1
                if target.stage_path and snapshot(target.stage_path) is not None:
                    _remove_owned_tree(target.stage_path, target.deployed_manifest,
                                       target.stage_identity, "事务暂存目录",
                                       allow_subset=True)
                self.journal.set_target(batch_id, target.selection_id, phase="finalized")
            self.journal.set_batch(batch_id, status="finalized")
            return DeploymentResult(batch_id, "success", "finalized",
                                    committed_targets=cleaned,
                                    duration_seconds=time.monotonic() - started)
        except (OSError, ValueError, SyncError) as exc:
            try:
                self.journal.set_batch(batch_id, status="finalize_required", error=str(exc))
            except JournalError:
                pass
            return DeploymentResult(batch_id, "failed", "finalize_required",
                                    committed_targets=cleaned, errors=(str(exc),),
                                    duration_seconds=time.monotonic() - started)
        finally:
            if process_locks is not None:
                process_locks.release()
            self._lock.release()
