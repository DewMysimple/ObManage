"""Read-only discovery of Obsidian vaults.

Discovery never follows links or reparse points.  A directory is a vault only
when it contains a real, readable, ordinary ``.obsidian`` directory.
"""
from __future__ import annotations

from ..scanning.io import scandir
from ..scanning.metrics import measured, scan_task

import json
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from threading import Event

from ..models import Progress, SyncCancelled, SyncError
from ..scanning.session import ScanSession, ScanPathError
from ..paths import assert_plain_chain, canonical, is_reparse, native, snapshot
from .models import ManagementIssue, OpenedVaultCandidates, VaultCatalogResult, VaultInfo

ProgressCallback = Callable[[Progress], None]
_RESERVED_DIRECTORY_PREFIXES = (".obmanage-deploy-",)


def _cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("操作已取消。")


def _sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _issue_sort_key(issue: ManagementIssue) -> tuple[str, str, str]:
    return ((issue.path or "").casefold(), issue.code, issue.message)


def _path_key(path: str) -> str:
    return os.path.normcase(path)


def _same_name(left: str, right: str) -> bool:
    return os.path.normcase(left) == os.path.normcase(right)


def _physical_plain_directory(path: str | Path) -> tuple[str | None, ManagementIssue | None]:
    """Resolve aliases only after the selected chain has been proven link-free."""
    selected = canonical(path)
    try:
        state = assert_plain_chain(selected, leaf_snapshotter=snapshot)
    except (OSError, SyncError) as exc:
        return None, ManagementIssue("unsafe_path", f"无法安全检查目录：{exc}", selected)
    if state is None:
        return None, ManagementIssue("missing_path", "目录不存在。", selected)
    if state["kind"] != "dir":
        return None, ManagementIssue("unsafe_path", "路径不是普通目录或包含链接。", selected)

    try:
        physical = canonical(os.path.realpath(native(selected)))
    except OSError as exc:
        return None, ManagementIssue("unsafe_path", f"无法解析目录的物理路径：{exc}", selected)
    if _path_key(physical) == _path_key(selected):
        # ``selected`` was just validated component by component.  Repeating the
        # identical chain walk for the common no-alias case doubles metadata I/O.
        physical_state = state
    else:
        try:
            physical_state = assert_plain_chain(physical, leaf_snapshotter=snapshot)
        except (OSError, SyncError) as exc:
            return None, ManagementIssue("unsafe_path", f"无法安全检查目录：{exc}", selected)
    if physical_state is None or physical_state["kind"] != "dir":
        return None, ManagementIssue("unsafe_path", "目录在检查期间已改变。", selected)
    return physical, None


@measured("marker_probe", calls=True)
def _marker_state(directory: str) -> tuple[str, ManagementIssue | None]:
    """Return absent/valid/unreadable/invalid for a possible marker."""
    marker = canonical(os.path.join(directory, ".obsidian"))
    try:
        state = snapshot(marker)
    except OSError as exc:
        return "unreadable", ManagementIssue(
            "marker_unreadable", f"无法检查 .obsidian 目录：{exc}", marker
        )
    if state is None:
        return "absent", None
    if state["kind"] != "dir":
        return "invalid", ManagementIssue(
            "invalid_marker", ".obsidian 必须是普通目录，不能是文件、链接或特殊项。", marker
        )
    try:
        assert_plain_chain(marker)
        # Opening the directory is intentional: a marker that merely has readable
        # metadata but cannot be enumerated is not a usable vault marker.
        with scandir(native(marker)):
            pass
    except (OSError, SyncError) as exc:
        return "unreadable", ManagementIssue(
            "marker_unreadable", f"无法读取 .obsidian 目录：{exc}", marker
        )
    return "valid", None


def _scope_issue(path: str) -> ManagementIssue | None:
    """Reject explicit discovery roots inside trash or app recovery directories."""
    current = path
    while True:
        if any(os.path.basename(current).casefold().startswith(prefix)
               for prefix in _RESERVED_DIRECTORY_PREFIXES):
            return ManagementIssue(
                "recovery_scope_excluded", "应用恢复目录不能作为仓库候选。", path, "warning",
            )
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if _same_name(os.path.basename(current), ".trash"):
            marker_status, _marker_issue = _marker_state(parent)
            if marker_status in ("valid", "unreadable"):
                return ManagementIssue(
                    "trash_scope_excluded",
                    "搜索范围位于仓库根级 .trash 内，已按已删除内容排除。",
                    path,
                    "warning",
                )
        current = parent


def _with_parents(paths: Iterable[str]) -> tuple[VaultInfo, ...]:
    ordered = sorted(set(paths), key=_sort_key)
    by_key = {_path_key(path): path for path in ordered}
    result: list[VaultInfo] = []
    for path in ordered:
        parent = None
        ancestor = os.path.dirname(path)
        while ancestor != os.path.dirname(ancestor):
            if _path_key(ancestor) in by_key:
                parent = by_key[_path_key(ancestor)]
                break
            ancestor = os.path.dirname(ancestor)
        if parent is None:
            parent = by_key.get(_path_key(ancestor)) if ancestor != path else None
        result.append(VaultInfo(path=path, name=Path(path).name or path, parent_path=parent))
    return tuple(result)


def _coerce_roots(roots: Iterable[str | Path] | str | Path) -> list[str | Path]:
    if isinstance(roots, (str, Path)):
        return [roots]
    return list(roots)


def _read_stable_file(path: str, expected: dict) -> bytes:
    """Read one ordinary file while proving its identity and content epoch."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(native(path), flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("配置路径不再是普通文件")
        expected_epoch = (
            expected["device"], expected["inode"], expected["size"],
            expected["mtime_ns"],
        )
        opened_epoch = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns,
        )
        if opened_epoch != expected_epoch:
            raise OSError("配置文件在读取前已被替换或改变")
        chunks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            chunks.append(block)
        after = os.fstat(descriptor)
        if (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        ) != (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        ):
            raise OSError("配置文件在读取期间发生变化")
    finally:
        os.close(descriptor)
    current = snapshot(path)
    if current is None or (
        current["kind"], current["device"], current["inode"],
        current["size"], current["mtime_ns"],
    ) != (
        expected["kind"], expected["device"], expected["inode"],
        expected["size"], expected["mtime_ns"],
    ):
        raise OSError("配置文件在读取期间被替换或改变")
    return b"".join(chunks)


def read_opened_vault_candidates(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> OpenedVaultCandidates:
    """Read only ``open: true`` entries for the independent occupancy check."""
    return _read_vault_candidates(config_path, environ=environ, opened_only=True)


def read_registered_vault_candidates(
    config_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> OpenedVaultCandidates:
    """Read all registered vaults, including closed ones, for source selection."""
    return _read_vault_candidates(config_path, environ=environ, opened_only=False)


def _read_vault_candidates(
    config_path: str | Path | None, *, environ: Mapping[str, str] | None,
    opened_only: bool,
) -> OpenedVaultCandidates:
    """Parse Obsidian's registry without granting filesystem write access.

    The configuration and every candidate are only read.  Candidate paths are
    intentionally not accepted as vaults until :func:`discover_vaults` validates
    their filesystem state.
    """
    issues: list[ManagementIssue] = []
    if config_path is None:
        env = os.environ if environ is None else environ
        appdata = env.get("APPDATA")
        if not appdata:
            issue = ManagementIssue(
                "appdata_missing", "无法定位 Obsidian 配置：环境变量 APPDATA 未设置。"
            )
            return OpenedVaultCandidates(issues=(issue,))
        config_path = Path(appdata) / "obsidian" / "obsidian.json"

    config = canonical(config_path)
    try:
        assert_plain_chain(os.path.dirname(config))
        state = snapshot(config)
    except (OSError, SyncError) as exc:
        issue = ManagementIssue("config_unreadable", f"无法安全检查 Obsidian 配置：{exc}", config)
        return OpenedVaultCandidates(issues=(issue,))
    if state is None:
        issue = ManagementIssue("config_missing", "未找到 Obsidian 配置文件。", config, "warning")
        return OpenedVaultCandidates(issues=(issue,))
    if state["kind"] != "file":
        issue = ManagementIssue(
            "config_unsafe", "Obsidian 配置必须是普通文件，不能是链接或特殊项。", config
        )
        return OpenedVaultCandidates(issues=(issue,))

    try:
        raw = _read_stable_file(config, state)
        payload = json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        issue = ManagementIssue(
            "config_invalid_json", f"Obsidian 配置 JSON 无效（第 {exc.lineno} 行，第 {exc.colno} 列）。", config
        )
        return OpenedVaultCandidates(issues=(issue,))
    except UnicodeDecodeError as exc:
        issue = ManagementIssue("config_invalid_encoding", f"Obsidian 配置不是有效 UTF-8：{exc}", config)
        return OpenedVaultCandidates(issues=(issue,))
    except OSError as exc:
        issue = ManagementIssue("config_unreadable", f"无法读取 Obsidian 配置：{exc}", config)
        return OpenedVaultCandidates(issues=(issue,))

    if not isinstance(payload, dict) or not isinstance(payload.get("vaults"), dict):
        issue = ManagementIssue("config_invalid_schema", "Obsidian 配置缺少 vaults 对象。", config)
        return OpenedVaultCandidates(issues=(issue,))

    found: dict[str, str] = {}
    for entry_id, entry in payload["vaults"].items():
        if not isinstance(entry, dict):
            issues.append(ManagementIssue(
                "config_invalid_entry", f"仓库记录 {entry_id!s} 不是对象。", config
            ))
            continue
        if opened_only and entry.get("open") is not True:
            continue
        value = entry.get("path")
        if not isinstance(value, str) or not value.strip():
            issues.append(ManagementIssue(
                "config_invalid_entry", f"仓库记录 {entry_id!s} 缺少有效路径。", config
            ))
            continue
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not os.path.isabs(expanded):
            issues.append(ManagementIssue(
                "config_relative_path", f"仓库记录 {entry_id!s} 使用了相对路径。", value
            ))
            continue
        candidate = canonical(expanded)
        found.setdefault(_path_key(candidate), candidate)

    paths = tuple(sorted(found.values(), key=_sort_key))
    return OpenedVaultCandidates(paths=paths, issues=tuple(sorted(issues, key=_issue_sort_key)))


@measured("discovery")
def _discover_vaults(
    roots: Iterable[str | Path] | str | Path,
    *,
    include_opened: bool = False,
    obsidian_config_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    cancel: Event | None = None,
    progress: ProgressCallback | None = None,
    recursive: bool = True,
    _session: ScanSession,
) -> VaultCatalogResult:
    """Discover unique physical vaults beneath one or more search roots."""
    root_values = _coerce_roots(roots)
    issues: list[ManagementIssue] = []
    if include_opened:
        opened = read_opened_vault_candidates(obsidian_config_path, environ=environ)
        root_values.extend(opened.paths)
        issues.extend(opened.issues)

    prepared: dict[str, str] = {}
    for raw_root in root_values:
        _cancelled(cancel)
        physical, issue = _physical_plain_directory(raw_root)
        if issue is not None:
            issues.append(issue)
            continue
        assert physical is not None
        scope_issue = _scope_issue(physical)
        if scope_issue is not None:
            issues.append(scope_issue)
            continue
        prepared.setdefault(_path_key(physical), physical)

    found: dict[str, str] = {}
    scanned: set[str] = set()
    completed_dirs = 0
    # Reverse push keeps the depth-first traversal stable while using a stack.
    stack = list(reversed(sorted(prepared.values(), key=_sort_key)))
    while stack:
        _cancelled(cancel)
        current = stack.pop()
        # Prepared roots are physical paths. Descendants use names returned by
        # their physical parent, so they cannot introduce an 8.3 alias. The live
        # chain and directory identity are checked by observe below; resolving
        # the identical physical path again at every depth adds no protection.
        physical = current
        key = _path_key(physical)
        if key in scanned:
            continue
        scanned.add(key)
        completed_dirs += 1

        observation = None
        if recursive:
            try:
                observation = _session.observe(physical, full=False, snapshotter=snapshot)
            except ScanPathError as exc:
                issues.append(ManagementIssue(exc.code, str(exc), exc.path))
                continue
            except (OSError, SyncError) as exc:
                issues.append(ManagementIssue("directory_unreadable", f"无法读取目录：{exc}", physical))
        # Enumerated names avoid an absent-marker query for every ordinary folder.
        # Exact registered roots keep the old narrow marker-only check.
        candidate = observation is None or any(_same_name(e.name, ".obsidian") for e in observation.entries)
        marker_status, marker_issue = _marker_state(physical) if candidate else ("absent", None)
        _session.markers[key] = (marker_status, marker_issue)
        if marker_issue is not None:
            issues.append(marker_issue)
        if marker_status == "valid":
            found.setdefault(key, physical)
            _session.vault_roots.add(key)
        if progress is not None:
            progress(Progress(phase="catalog", message="正在发现 Obsidian 仓库",
                              relative_path=physical, completed_files=completed_dirs))
        if not recursive or observation is None:
            continue
        _session.discovery_keys.add(key)
        issues.extend(ManagementIssue(i.code, i.message, i.path) for i in observation.issues)
        issues.extend(ManagementIssue("ambiguous_name", "存在 Windows 无法区分的大小写重名路径。",
            os.path.join(physical, name)) for name in observation.name_conflicts())
        if _session.capture and _session.inside_vault(physical):
            try:
                observation = _session.observe(physical, snapshotter=snapshot,
                    chain_validated=True, folder_state=observation.state)
            except (OSError, SyncError) as exc:
                issues.append(ManagementIssue("directory_unreadable", f"无法读取目录：{exc}", physical))
                continue
        children = []
        for entry in observation.entries:
            _cancelled(cancel)
            if _same_name(entry.name, ".obsidian"):
                continue
            if marker_status in ("valid", "unreadable") and _same_name(entry.name, ".trash"):
                continue
            if any(entry.name.casefold().startswith(prefix) for prefix in _RESERVED_DIRECTORY_PREFIXES):
                continue
            child = canonical(os.path.join(physical, entry.name))
            if entry.state is None:
                continue
            if entry.state["kind"] == "dir":
                children.append(child)
            elif entry.state["kind"] != "file":
                issues.append(ManagementIssue("unsafe_item", "已跳过链接、重解析点或特殊项。", child))
        stack.extend(reversed(sorted(children, key=_sort_key)))

    vaults = _with_parents(found.values())
    return VaultCatalogResult(vaults=vaults, issues=tuple(sorted(issues, key=_issue_sort_key)))


@scan_task("catalog")
def discover_vaults(roots, *, include_opened=False, obsidian_config_path=None,
                    environ=None, cancel=None, progress=None, recursive=True,
                    _session=None) -> VaultCatalogResult:
    """Discover live roots; an optional task session shares initial observations."""
    arguments = dict(include_opened=include_opened, obsidian_config_path=obsidian_config_path,
                     environ=environ, cancel=cancel, progress=progress, recursive=recursive)
    if _session is not None:
        return _discover_vaults(roots, _session=_session, **arguments)
    with ScanSession(cancel) as session:
        return _discover_vaults(roots, _session=session, **arguments)
