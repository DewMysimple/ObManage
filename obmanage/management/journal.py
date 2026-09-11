"""Persistent transaction journal for bounded subtree deployments.

The journal deliberately stores portable tree manifests in addition to paths and
phases.  A later process can therefore prove that a backup or deployed tree is
still the one created by the recorded transaction before it performs cleanup or
rollback.  Journal files are never used as authority for arbitrary paths: the
deployment engine independently validates every owned path against the batch
UUID and selected target root.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from ..models import SyncError
from ..paths import assert_plain_chain, canonical, identity, native, snapshot


JOURNAL_VERSION = 1
_JOURNAL_LOCK = threading.RLock()
KEY_SIZE = 32
AUTHENTICATION_FIELD = "_authentication"
BATCH_STATUSES = frozenset({
    "preparing", "prepared", "committing", "committed", "failed", "cancelled",
    "prepare_failed", "recovery_required", "rolling_back", "rolled_back",
    "rolled_back_with_residuals",
    "rollback_blocked", "rollback_required", "finalizing", "finalized",
    "finalize_required", "commit_failed",
})
TARGET_PHASES = frozenset({
    "planned", "unchanged", "staging", "prepared", "backing_up", "backed_up",
    "installing", "committed", "cancelled", "prepare_failed",
    "rollback_moving_deployed", "rolled_back", "finalizing", "finalized",
})


class JournalError(SyncError):
    """The deployment journal is missing, corrupt, or unsafe to update."""


@dataclass(frozen=True)
class OwnedDirectory:
    """A directory created by a batch and its registered filesystem identity."""

    path: str
    identity: tuple[str, int, int] | None = None


@dataclass(frozen=True)
class JournalTarget:
    """Durable state for one component-to-vault selection."""

    selection_id: str
    component_id: str
    source_path: str
    source_authorization_root: str
    source_volume: str
    target_root: str
    target_relative: str
    target_path: str
    target_volume: str
    target_root_identity: tuple[str, int, int] | None = None
    phase: str = "planned"
    stage_path: str | None = None
    stage_identity: tuple[str, int, int] | None = None
    backup_path: str | None = None
    backup_identity: tuple[str, int, int] | None = None
    rollback_path: str | None = None
    rollback_identity: tuple[str, int, int] | None = None
    deployed_identity: tuple[str, int, int] | None = None
    original_identity: tuple[str, int, int] | None = None
    source_manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    original_manifest: dict[str, dict[str, Any]] | None = None
    deployed_manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_parents: tuple[OwnedDirectory, ...] = ()
    residuals: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class JournalBatch:
    """One durable multi-target deployment transaction."""

    batch_id: str
    label: str
    status: str
    targets: tuple[JournalTarget, ...]
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    revision: int = 0
    errors: tuple[str, ...] = ()
    version: int = JOURNAL_VERSION


def _identity_from_json(value: Any) -> tuple[str, int, int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 3:
        raise JournalError("事务日志中的目录身份无效。")
    return str(value[0]), int(value[1]), int(value[2])


def _owned_from_json(value: Any) -> OwnedDirectory:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        raise JournalError("事务日志中的已创建目录记录无效。")
    return OwnedDirectory(
        path=canonical(value["path"]),
        identity=_identity_from_json(value.get("identity")),
    )


def _relative_parts(value: Any, description: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise JournalError(f"事务日志中的{description}相对路径无效。")
    normalized = value.replace("\\", "/")
    parts = tuple(normalized.split("/"))
    if (os.path.isabs(value) or os.path.splitdrive(value)[0]
            or any(part in ("", ".", "..") or ":" in part for part in parts)):
        raise JournalError(f"事务日志中的{description}相对路径越界。")
    return parts


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.normpath(canonical(value))).casefold()


def _strict_child(root: str, path: str, description: str) -> tuple[str, ...]:
    root, path = canonical(root), canonical(path)
    try:
        relative = os.path.relpath(path, root)
    except ValueError as exc:
        raise JournalError(f"事务日志中的{description}不在目标仓库内。") from exc
    parts = _relative_parts(relative, description)
    rebuilt = canonical(os.path.join(root, *parts))
    if _path_key(rebuilt) != _path_key(path) or _path_key(path) == _path_key(root):
        raise JournalError(f"事务日志中的{description}不在目标仓库内。")
    try:
        if _path_key(os.path.commonpath((_path_key(root), _path_key(path)))) != _path_key(root):
            raise JournalError(f"事务日志中的{description}越出目标仓库。")
    except ValueError as exc:
        raise JournalError(f"事务日志中的{description}越出目标仓库。") from exc
    return parts


def _manifest_from_json(value: Any, *, optional: bool = False) -> dict[str, dict[str, Any]] | None:
    if value is None and optional:
        return None
    if not isinstance(value, dict):
        raise JournalError("事务日志中的目录清单无效。")
    result: dict[str, dict[str, Any]] = {}
    for relative, item in value.items():
        if not isinstance(relative, str) or not isinstance(item, dict):
            raise JournalError("事务日志中的目录清单条目无效。")
        kind = item.get("kind")
        if kind not in ("file", "dir"):
            raise JournalError("事务日志中的目录清单类型无效。")
        cleaned: dict[str, Any] = {"kind": kind}
        if kind == "file":
            digest = item.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                raise JournalError("事务日志中的文件摘要无效。")
            cleaned.update(size=int(item.get("size", -1)), sha256=digest)
            if cleaned["size"] < 0:
                raise JournalError("事务日志中的文件大小无效。")
        result[relative] = cleaned
    if result.get("") != {"kind": "dir"}:
        raise JournalError("事务日志中的目录清单缺少根目录。")
    return result


def _target_from_json(value: Any) -> JournalTarget:
    if not isinstance(value, dict):
        raise JournalError("事务日志中的目标记录无效。")
    required = (
        "selection_id", "component_id", "source_path", "source_volume",
        "source_authorization_root",
        "target_root", "target_relative", "target_path", "target_volume",
    )
    if any(not isinstance(value.get(name), str) or not value[name] for name in required):
        raise JournalError("事务日志中的目标字段不完整。")
    created = value.get("created_parents", [])
    if not isinstance(created, list):
        raise JournalError("事务日志中的父目录记录无效。")
    residuals = value.get("residuals", [])
    if not isinstance(residuals, list) or any(
            not isinstance(item, str) for item in residuals):
        raise JournalError("事务日志中的残留目录记录无效。")
    try:
        selection_id = str(uuid.UUID(value["selection_id"]))
    except (ValueError, AttributeError) as exc:
        raise JournalError("事务日志中的目标编号无效。") from exc
    if selection_id != value["selection_id"]:
        raise JournalError("事务日志中的目标编号格式无效。")
    source_path = canonical(value["source_path"])
    source_authorization_root = canonical(value["source_authorization_root"])
    try:
        source_common = _path_key(os.path.commonpath((
            _path_key(source_authorization_root), _path_key(source_path),
        )))
    except ValueError as exc:
        raise JournalError("事务日志中的来源越出授权根目录。") from exc
    if source_common != _path_key(source_authorization_root):
        raise JournalError("事务日志中的来源越出授权根目录。")
    target_root = canonical(value["target_root"])
    target_path = canonical(value["target_path"])
    target_parts = _relative_parts(value["target_relative"], "目标")
    if _path_key(canonical(os.path.join(target_root, *target_parts))) != _path_key(target_path):
        raise JournalError("事务日志中的目标绝对路径与限定相对路径不一致。")
    _strict_child(target_root, target_path, "目标")
    stage_path = canonical(value["stage_path"]) if value.get("stage_path") else None
    backup_path = canonical(value["backup_path"]) if value.get("backup_path") else None
    rollback_path = canonical(value["rollback_path"]) if value.get("rollback_path") else None
    for path, description in ((stage_path, "暂存路径"), (backup_path, "备份路径"),
                              (rollback_path, "回滚隔离路径")):
        if path is not None and len(_strict_child(target_root, path, description)) != 1:
            raise JournalError(f"事务日志中的{description}必须是目标仓库的直接子目录。")
    created_parents = tuple(_owned_from_json(item) for item in created)
    allowed_parents = {
        _path_key(canonical(os.path.join(target_root, *target_parts[:index])))
        for index in range(1, len(target_parts))
    }
    seen_parents: set[str] = set()
    previous_depth = 0
    for parent in created_parents:
        key = _path_key(parent.path)
        parts = _strict_child(target_root, parent.path, "已创建父目录")
        if key not in allowed_parents or key in seen_parents or len(parts) <= previous_depth:
            raise JournalError("事务日志中的已创建父目录与限定目标不一致。")
        if parent.identity is None or parent.identity[0] != "dir":
            raise JournalError("事务日志中的已创建父目录身份无效。")
        seen_parents.add(key)
        previous_depth = len(parts)
    stage_identity = _identity_from_json(value.get("stage_identity"))
    backup_identity = _identity_from_json(value.get("backup_identity"))
    rollback_identity = _identity_from_json(value.get("rollback_identity"))
    target_root_identity = _identity_from_json(value.get("target_root_identity"))
    original_identity = _identity_from_json(value.get("original_identity"))
    deployed_identity = _identity_from_json(value.get("deployed_identity"))
    phase = str(value.get("phase", "planned"))
    if phase not in TARGET_PHASES:
        raise JournalError("事务日志中的目标阶段无效。")
    if target_root_identity is None or target_root_identity[0] != "dir":
        raise JournalError("事务日志中的目标仓库身份无效。")
    if stage_path is not None and (stage_identity is None or stage_identity[0] != "dir"):
        raise JournalError("事务日志中的暂存目录缺少可信身份。")
    if backup_path is not None and (backup_identity is None or backup_identity[0] != "dir"):
        raise JournalError("事务日志中的备份目录缺少可信身份。")
    if rollback_path is not None and (rollback_identity is None or rollback_identity[0] != "dir"):
        raise JournalError("事务日志中的回滚隔离目录缺少可信身份。")
    original_manifest = _manifest_from_json(value.get("original_manifest"), optional=True)
    if original_manifest is not None and (original_identity is None or original_identity[0] != "dir"):
        raise JournalError("事务日志中的原目标目录缺少可信身份。")
    return JournalTarget(
        selection_id=selection_id,
        component_id=value["component_id"],
        source_path=source_path,
        source_authorization_root=source_authorization_root,
        source_volume=value["source_volume"],
        target_root=target_root,
        target_relative="/".join(target_parts),
        target_path=target_path,
        target_volume=value["target_volume"],
        target_root_identity=target_root_identity,
        phase=phase,
        stage_path=stage_path,
        stage_identity=stage_identity,
        backup_path=backup_path,
        backup_identity=backup_identity,
        rollback_path=rollback_path,
        rollback_identity=rollback_identity,
        deployed_identity=deployed_identity,
        original_identity=original_identity,
        source_manifest=_manifest_from_json(value.get("source_manifest")) or {},
        original_manifest=original_manifest,
        deployed_manifest=_manifest_from_json(value.get("deployed_manifest")) or {},
        created_parents=created_parents,
        residuals=tuple(residuals),
        error=str(value.get("error", "")),
    )


def _batch_from_json(value: Any) -> JournalBatch:
    if not isinstance(value, dict):
        raise JournalError("事务日志不是有效对象。")
    try:
        batch_id = str(uuid.UUID(str(value["batch_id"])))
    except (KeyError, ValueError, AttributeError) as exc:
        raise JournalError("事务日志中的批次编号无效。") from exc
    if int(value.get("version", -1)) != JOURNAL_VERSION:
        raise JournalError("事务日志版本不受支持。")
    targets = value.get("targets")
    if not isinstance(targets, list) or not targets:
        raise JournalError("事务日志中的目标列表无效。")
    errors = value.get("errors", [])
    if not isinstance(errors, list):
        raise JournalError("事务日志中的错误列表无效。")
    status = str(value.get("status", "unknown"))
    if status not in BATCH_STATUSES:
        raise JournalError("事务日志中的批次状态无效。")
    parsed_targets = tuple(_target_from_json(item) for item in targets)
    terminal_phases = {
        "prepared": frozenset({"prepared", "unchanged"}),
        "committed": frozenset({"committed", "unchanged"}),
        "cancelled": frozenset({"cancelled"}),
        "prepare_failed": frozenset({"prepare_failed"}),
        "rolled_back": frozenset({"rolled_back"}),
        "rolled_back_with_residuals": frozenset({"rolled_back"}),
        "finalized": frozenset({"finalized"}),
    }
    allowed_terminal = terminal_phases.get(status)
    if allowed_terminal is not None and any(
            target.phase not in allowed_terminal for target in parsed_targets):
        raise JournalError("事务日志的批次状态与目标阶段不一致。")
    selection_ids = [item.selection_id for item in parsed_targets]
    if len(selection_ids) != len(set(selection_ids)):
        raise JournalError("事务日志中存在重复的目标编号。")
    for index, target in enumerate(parsed_targets):
        for other in parsed_targets[index + 1:]:
            first_root, first_path = _path_key(target.target_root), _path_key(target.target_path)
            second_root, second_path = _path_key(other.target_root), _path_key(other.target_path)
            try:
                same_drive = os.path.splitdrive(first_path)[0] == os.path.splitdrive(second_path)[0]
                if same_drive:
                    common = _path_key(os.path.commonpath((first_path, second_path)))
                    if common in (first_path, second_path):
                        raise JournalError("事务日志中的限定目标相同或互相包含。")
            except ValueError:
                pass
            if (first_root == second_root and first_path == second_path):
                raise JournalError("事务日志中的限定目标重复。")
    return JournalBatch(
        batch_id=batch_id,
        label=str(value.get("label", "")),
        status=status,
        targets=parsed_targets,
        created_at=float(value.get("created_at", 0.0)),
        updated_at=float(value.get("updated_at", 0.0)),
        revision=int(value.get("revision", 0)),
        errors=tuple(str(item) for item in errors),
        version=JOURNAL_VERSION,
    )


def _grant_payload(record: JournalBatch) -> dict[str, Any]:
    """Return every immutable authority field covered by the batch MAC."""
    return {
        "version": JOURNAL_VERSION,
        "batch_id": record.batch_id,
        "label": record.label,
        "created_at": record.created_at,
        "targets": [{
            "selection_id": target.selection_id,
            "component_id": target.component_id,
            "source_path": target.source_path,
            "source_authorization_root": target.source_authorization_root,
            "source_volume": target.source_volume,
            "target_root": target.target_root,
            "target_relative": target.target_relative,
            "target_path": target.target_path,
            "target_volume": target.target_volume,
            "target_root_identity": target.target_root_identity,
            "original_identity": target.original_identity,
            "source_manifest": target.source_manifest,
            "original_manifest": target.original_manifest,
            "deployed_manifest": target.deployed_manifest,
        } for target in record.targets],
    }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _record_payload(record: JournalBatch) -> dict[str, Any]:
    """Return a JSON-native representation covered by the mutable record MAC."""
    return json.loads(_canonical_json(asdict(record)).decode("utf-8"))


class DeploymentJournal:
    """Atomic JSON storage with fresh-read updates for cross-instance recovery."""

    def __init__(self, state_dir: str | Path):
        requested = canonical(state_dir)
        assert_plain_chain(requested)
        self.state_dir = canonical(os.path.realpath(native(requested)))
        assert_plain_chain(self.state_dir)
        self.directory = canonical(os.path.join(self.state_dir, "deployment-journal"))
        self.grant_directory = canonical(os.path.join(self.state_dir, "deployment-grants"))
        self.key_path = canonical(os.path.join(self.state_dir, "deployment-journal.key"))

    @staticmethod
    def _valid_batch_id(batch_id: str) -> str:
        try:
            canonical_id = str(uuid.UUID(str(batch_id)))
        except (ValueError, AttributeError) as exc:
            raise JournalError("批次编号无效。") from exc
        if canonical_id != str(batch_id).lower():
            raise JournalError("批次编号格式无效。")
        return canonical_id

    def _path(self, batch_id: str) -> str:
        return canonical(os.path.join(self.directory, f"{self._valid_batch_id(batch_id)}.json"))

    def _grant_path(self, batch_id: str) -> str:
        return canonical(os.path.join(self.grant_directory,
                                      f"{self._valid_batch_id(batch_id)}.grant.json"))

    def _ensure_directory(self) -> None:
        for directory in (self.state_dir, self.directory, self.grant_directory):
            assert_plain_chain(directory)
            os.makedirs(native(directory), mode=0o700, exist_ok=True)
            assert_plain_chain(directory)
            try:
                os.chmod(native(directory), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                # Windows ACLs are inherited from the user's local app-data
                # directory; chmod still tightens DOS/POSIX modes where honored.
                if os.name != "nt":
                    raise

    @staticmethod
    def _read_regular_bytes(path: str, description: str) -> bytes:
        try:
            assert_plain_chain(os.path.dirname(path))
        except SyncError as exc:
            raise JournalError(f"{description}的父路径不安全。") from exc
        expected = snapshot(path)
        if expected is None or expected["kind"] != "file":
            raise JournalError(f"{description}不是普通文件。")
        try:
            with open(native(path), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or ("file", opened.st_dev, opened.st_ino) != identity(expected)):
                    raise JournalError(f"打开{description}时文件身份已改变。")
                data = stream.read()
                finished = os.fstat(stream.fileno())
                if (finished.st_dev, finished.st_ino, finished.st_size) != (
                        opened.st_dev, opened.st_ino, opened.st_size):
                    raise JournalError(f"读取{description}期间文件发生变化。")
                return data
        except OSError as exc:
            raise JournalError(f"无法读取{description}：{exc}") from exc

    def _read_key(self) -> bytes:
        assert_plain_chain(os.path.dirname(self.key_path))
        if snapshot(self.key_path) is None:
            raise JournalError("部署授权密钥缺失。")
        key = self._read_regular_bytes(self.key_path, "部署授权密钥")
        if len(key) != KEY_SIZE:
            raise JournalError("部署授权密钥无效。")
        return key

    def _has_persistent_authority(self) -> bool:
        """Return whether any durable item may depend on the current key."""
        for directory in (self.directory, self.grant_directory):
            state = snapshot(directory)
            if state is None:
                continue
            if state["kind"] != "dir":
                raise JournalError("部署事务目录不安全。")
            try:
                assert_plain_chain(directory)
                with os.scandir(native(directory)) as iterator:
                    if next(iterator, None) is not None:
                        return True
            except OSError as exc:
                raise JournalError(f"无法检查现有部署授权：{exc}") from exc
        return False

    def _publish_key(self) -> None:
        """Fully write and fsync a key before atomically publishing its name."""
        key = secrets.token_bytes(KEY_SIZE)
        temporary = canonical(self.key_path + f".tmp-{uuid.uuid4()}")
        descriptor: int | None = None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(native(temporary), flags,
                                 stat.S_IRUSR | stat.S_IWUSR)
            view = memoryview(key)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("无法写入部署授权密钥。")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.chmod(native(temporary), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                if os.name != "nt":
                    raise
            # Publish without overwriting a key from a concurrent legitimate
            # creator.  Windows rename is atomic and no-replace; POSIX hard
            # link creation supplies the same no-replace guarantee for a
            # fully-written same-directory file.
            try:
                if os.name == "nt":
                    os.rename(native(temporary), native(self.key_path))
                    temporary = ""
                else:
                    os.link(native(temporary), native(self.key_path))
            except FileExistsError:
                return
            try:
                os.chmod(native(self.key_path), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                if os.name != "nt":
                    raise
        except OSError as exc:
            raise JournalError(f"无法创建部署授权密钥：{exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(native(temporary))
                except FileNotFoundError:
                    pass

    def _get_or_create_key(self) -> bytes:
        self._ensure_directory()
        key_state = snapshot(self.key_path)
        if key_state is not None:
            try:
                return self._read_key()
            except JournalError:
                if self._has_persistent_authority():
                    raise JournalError(
                        "部署授权密钥无效，已有事务记录时拒绝重建密钥。"
                    )
                # Repair only a plain, unshared regular file when no durable
                # journal/grant can depend on its incomplete bytes.
                if key_state["kind"] != "file":
                    raise
                try:
                    current = os.lstat(native(self.key_path))
                except OSError as exc:
                    raise JournalError(f"无法检查残缺的部署授权密钥：{exc}") from exc
                if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                        or identity(key_state) != ("file", current.st_dev, current.st_ino)):
                    raise JournalError("残缺的部署授权密钥身份不安全。")
                try:
                    os.unlink(native(self.key_path))
                except OSError as exc:
                    raise JournalError(f"无法移除残缺的部署授权密钥：{exc}") from exc
                if snapshot(self.key_path) is not None:
                    raise JournalError("无法安全移除残缺的部署授权密钥。")
        elif self._has_persistent_authority():
            # Losing the key must not silently mint a new authority over old
            # records, including orphan temp/grant artifacts from a crash.
            raise JournalError("部署授权密钥缺失，已有事务记录时拒绝重建密钥。")
        self._publish_key()
        return self._read_key()

    def _create_grant(self, record: JournalBatch, key: bytes) -> None:
        path = self._grant_path(record.batch_id)
        if snapshot(path) is not None:
            raise JournalError("部署批次授权锚点已存在。")
        payload = _grant_payload(record)
        document = {
            "version": JOURNAL_VERSION,
            "grant": payload,
            "mac": hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest(),
        }
        data = _canonical_json(document) + b"\n"
        descriptor: int | None = None
        try:
            descriptor = os.open(native(path),
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_BINARY", 0),
                                 stat.S_IRUSR | stat.S_IWUSR)
            written = 0
            while written < len(data):
                written += os.write(descriptor, data[written:])
            os.fsync(descriptor)
        except OSError as exc:
            raise JournalError(f"无法创建部署批次授权锚点：{exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            os.chmod(native(path), stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            if os.name != "nt":
                raise

    def _verify_existing_authority(self) -> None:
        """Require a complete, currently authenticated journal/grant set."""
        identifiers: dict[str, set[str]] = {}
        for label, directory, suffix in (
                ("journal", self.directory, ".json"),
                ("grant", self.grant_directory, ".grant.json")):
            state = snapshot(directory)
            if state is None:
                identifiers[label] = set()
                continue
            if state["kind"] != "dir":
                raise JournalError("部署授权目录不安全。")
            try:
                assert_plain_chain(directory)
                names = tuple(os.listdir(native(directory)))
            except OSError as exc:
                raise JournalError(f"无法检查现有部署授权：{exc}") from exc
            values: set[str] = set()
            for name in names:
                if not name.endswith(suffix):
                    continue
                batch_id = self._valid_batch_id(name[:-len(suffix)])
                values.add(batch_id)
            identifiers[label] = values
        if identifiers["journal"] != identifiers["grant"]:
            raise JournalError("部署事务日志与授权锚点不完整，拒绝创建新批次。")
        if not identifiers["journal"]:
            # A present key is itself persistent security state.  Listing is
            # the UI recovery probe, so expose a damaged/partial key here even
            # when no complete batch survived.
            if snapshot(self.key_path) is not None:
                self._read_key()
            return
        for batch_id in sorted(identifiers["journal"]):
            self._read_path(self._path(batch_id))

    def _verify_grant(self, record: JournalBatch) -> None:
        key = self._read_key()
        path = self._grant_path(record.batch_id)
        try:
            document = json.loads(self._read_regular_bytes(path, "部署批次授权锚点").decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise JournalError(f"部署批次授权锚点损坏：{exc}") from exc
        if not isinstance(document, dict) or document.get("version") != JOURNAL_VERSION:
            raise JournalError("部署批次授权锚点版本无效。")
        grant, supplied_mac = document.get("grant"), document.get("mac")
        if not isinstance(grant, dict) or not isinstance(supplied_mac, str):
            raise JournalError("部署批次授权锚点结构无效。")
        expected_mac = hmac.new(key, _canonical_json(grant), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_mac, supplied_mac):
            raise JournalError("部署批次授权锚点认证失败。")
        current_grant = json.loads(_canonical_json(_grant_payload(record)).decode("utf-8"))
        if grant != current_grant:
            raise JournalError("部署事务日志超出已认证的路径或快照授权。")

    def _verify_record_authentication(self, payload: dict[str, Any],
                                      authentication: Any) -> None:
        if (not isinstance(authentication, dict)
                or authentication.get("algorithm") != "hmac-sha256"
                or not isinstance(authentication.get("mac"), str)):
            raise JournalError("部署事务日志缺少完整认证。")
        supplied = authentication["mac"]
        expected = hmac.new(
            self._read_key(), _canonical_json(payload), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise JournalError("部署事务日志完整性认证失败。")

    def _read_path(self, path: str) -> JournalBatch:
        try:
            assert_plain_chain(os.path.dirname(path))
            value = json.loads(self._read_regular_bytes(path, "部署事务日志").decode("utf-8"))
        except FileNotFoundError as exc:
            raise JournalError("找不到指定的部署批次。") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JournalError(f"无法读取部署事务日志：{exc}") from exc
        if not isinstance(value, dict):
            raise JournalError("部署事务日志不是有效对象。")
        authentication = value.get(AUTHENTICATION_FIELD)
        payload = {
            name: item for name, item in value.items()
            if name != AUTHENTICATION_FIELD
        }
        # Schema parsing performs no target I/O.  It can therefore provide a
        # precise corrupt-field error before the whole-record MAC is checked.
        record = _batch_from_json(payload)
        self._verify_record_authentication(payload, authentication)
        if self._path(record.batch_id) != canonical(path):
            raise JournalError("事务日志文件名与批次编号不一致。")
        self._verify_grant(record)
        return record

    def _write(self, record: JournalBatch, *, must_not_exist: bool = False) -> None:
        # Validate the complete schema even for in-process dataclass callers.
        payload = _record_payload(record)
        _batch_from_json(payload)
        path = self._path(record.batch_id)
        self._ensure_directory()
        authentication = {
            "algorithm": "hmac-sha256",
            "mac": hmac.new(
                self._read_key(), _canonical_json(payload), hashlib.sha256
            ).hexdigest(),
        }
        document = dict(payload)
        document[AUTHENTICATION_FIELD] = authentication
        if must_not_exist and os.path.exists(native(path)):
            raise JournalError("部署批次编号已存在。")
        descriptor: int | None = None
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{record.batch_id}.", suffix=".tmp", dir=native(self.directory)
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = None
                json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(native(temporary), native(path))
            temporary = None
        except OSError as exc:
            raise JournalError(f"无法写入部署事务日志：{exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(native(temporary))
                except OSError:
                    pass

    def create(self, record: JournalBatch) -> JournalBatch:
        """Create a new batch record; existing UUIDs are never overwritten."""
        with _JOURNAL_LOCK:
            if record.version != JOURNAL_VERSION or record.revision != 0:
                raise JournalError("新事务日志的版本或修订号无效。")
            # Parse once before creating authority files, then persist the
            # immutable grant before the mutable phase journal.
            _batch_from_json(json.loads(_canonical_json(asdict(record)).decode("utf-8")))
            key = self._get_or_create_key()
            self._verify_existing_authority()
            self._create_grant(record, key)
            self._write(record, must_not_exist=True)
            return record

    def prepare_new_batch(self) -> None:
        """Safely establish and verify authority before new-batch checks.

        This may repair a legacy partial key only when no journal or grant
        exists.  Any existing authority must authenticate with the current key.
        """
        with _JOURNAL_LOCK:
            self._get_or_create_key()
            self._verify_existing_authority()

    def get(self, batch_id: str) -> JournalBatch:
        """Load one batch from disk without relying on an instance cache."""
        with _JOURNAL_LOCK:
            return self._read_path(self._path(batch_id))

    def list(self) -> tuple[JournalBatch, ...]:
        """Return every valid journal in newest-first order."""
        with _JOURNAL_LOCK:
            # Verify the full journal/grant/key set before an empty result can
            # tell the UI that recovery is clear.  Orphan anchors and a damaged
            # key are recovery-blocking state, not an empty history.
            self._verify_existing_authority()
            directory_state = snapshot(self.directory)
            if directory_state is None:
                return ()
            if directory_state["kind"] != "dir":
                raise JournalError("部署事务日志目录不安全。")
            try:
                assert_plain_chain(self.directory)
            except SyncError as exc:
                raise JournalError("部署事务日志目录不安全。") from exc
            records: list[JournalBatch] = []
            try:
                names = tuple(os.listdir(native(self.directory)))
            except OSError as exc:
                raise JournalError(f"无法列出部署事务日志：{exc}") from exc
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    batch_id = name[:-5]
                    path = self._path(batch_id)
                except JournalError as exc:
                    raise JournalError(f"发现无法认证的事务日志文件：{name}") from exc
                records.append(self._read_path(path))
            return tuple(sorted(records, key=lambda item: (item.created_at, item.batch_id), reverse=True))

    def update(self, batch_id: str, transform: Callable[[JournalBatch], JournalBatch]) -> JournalBatch:
        """Fresh-read, transform and atomically replace a batch record."""
        with _JOURNAL_LOCK:
            current = self._read_path(self._path(batch_id))
            changed = transform(current)
            if changed.batch_id != current.batch_id or changed.created_at != current.created_at:
                raise JournalError("事务日志更新不能改变批次身份。")
            if changed.version != JOURNAL_VERSION:
                raise JournalError("事务日志更新版本无效。")
            saved = replace(changed, revision=current.revision + 1, updated_at=time.time())
            self._verify_grant(saved)
            self._write(saved)
            return saved

    def set_batch(self, batch_id: str, *, status: str | None = None,
                  error: str | None = None) -> JournalBatch:
        def transform(record: JournalBatch) -> JournalBatch:
            errors = record.errors + ((error,) if error else ())
            return replace(record, status=status or record.status, errors=errors)

        return self.update(batch_id, transform)

    def set_target(self, batch_id: str, selection_id: str, **changes: Any) -> JournalBatch:
        """Update exactly one target while preserving all other fresh fields."""
        allowed = set(JournalTarget.__dataclass_fields__) - {"selection_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise JournalError(f"未知的目标日志字段：{', '.join(sorted(unknown))}")

        def transform(record: JournalBatch) -> JournalBatch:
            found = False
            targets: list[JournalTarget] = []
            for target in record.targets:
                if target.selection_id == selection_id:
                    found = True
                    targets.append(replace(target, **changes))
                else:
                    targets.append(target)
            if not found:
                raise JournalError("事务日志中找不到指定目标。")
            return replace(record, targets=tuple(targets))

        return self.update(batch_id, transform)
