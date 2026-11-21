"""Target-scoped Windows session closure and exact-path reopening, without Qt."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from ..models import Progress, SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, snapshot
from .catalog import _read_stable_file, discover_vaults
from .running_vaults import path_key, post_window_close, read_runtime_snapshot, vault_identity


class ObsidianLifecycleError(SyncError):
    """Obsidian's runtime state could not be changed or verified safely."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("已取消关闭目标仓库。")


def _runtime(paths, config_path, cancel=None):
    return read_runtime_snapshot(config_path, cancel=cancel, suspect_paths=paths)


def _require_reliable(runtime, paths):
    blockers = runtime.blockers(paths)
    if blockers:
        raise ObsidianLifecycleError("无法安全核验目标仓库会话：" + blockers[0].message)


def assert_target_sessions_closed(paths, *, config_path=None):
    """Commit barrier for the paths actually eligible for writing."""
    paths = tuple(paths)
    if not paths:
        return
    runtime = _runtime(paths, config_path)
    _require_reliable(runtime, paths)
    for path in paths:
        if runtime.session(path) is not None:
            raise ObsidianLifecycleError(f"目标仓库仍在运行或已重新打开：{path}")


def _close_session(path, original, config_path, cancel, on_closed, timeout):
    originals = {window.key for window in original.windows}
    posted = set()
    recorded = False
    send_error = None
    closed_since = None
    deadline = time.monotonic() + timeout
    while True:
        posted_now = False
        if not posted:
            _check_cancel(cancel)
        fresh = _runtime((path,), config_path)
        blockers = fresh.blockers((path,))
        active = fresh.session(path)
        disappeared = (fresh.windows is not None
                       and not originals.intersection(window.key for window in fresh.windows))
        now = time.monotonic()
        if disappeared and posted and not recorded:
            if on_closed is not None:
                on_closed(path)
            recorded = True
        if not blockers and disappeared and active is None:
            if not posted:
                return False
            if closed_since is None:
                closed_since = now
            # Let Electron finish the close event before analysis or reopening.
            if now >= min(deadline, closed_since + 0.2):
                return True
        else:
            closed_since = None

        if blockers and not posted and blockers[0].code != "runtime_changed":
            _require_reliable(fresh, (path,))
        if not blockers and active is not None:
            if active.directory_identity != original.directory_identity:
                raise ObsidianLifecycleError(f"目标仓库目录身份已改变：{path}")
            if any(window.key not in originals for window in active.windows):
                if not posted or disappeared:
                    raise ObsidianLifecycleError(f"目标仓库在关闭期间重新打开或更换窗口：{path}")
                send_error = "发现新窗口，无法安全完成关闭。"
            if send_error is None:
                # Ask the main window first where Windows provides ownership.
                # Independent, title-verified pop-outs may remain after it.
                ordered = sorted(active.windows, key=lambda window: bool(window.owner))
                next_window = next((window for window in ordered if window.key not in posted), None)
                if next_window is not None and (now < deadline or (timeout == 0 and not posted)):
                    try:
                        post_window_close(next_window)
                        posted.add(next_window.key)
                        posted_now = True
                    except OSError as exc:
                        if not posted:
                            raise ObsidianLifecycleError(f"无法关闭目标仓库 {path}：{exc}") from exc
                        send_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if posted_now:
                # Include at least one observation after every posted close.
                continue
            detail = blockers[0].message if blockers else send_error or "窗口未在限定时间内正常关闭。"
            raise ObsidianLifecycleError(f"无法确认目标仓库已关闭 {path}：{detail}")
        # Avoid posting a second close while the first is still unwinding its
        # owned windows and temporarily changing the native inventory.
        time.sleep(min(0.1, remaining))


def close_target_sessions(paths, *, config_path=None, cancel=None, progress=None, on_closed=None,
                          expected_identities=None, graceful_timeout=5.0):
    """Close only verified target windows and report each confirmed closure as it happens."""
    if graceful_timeout < 0:
        raise ValueError("等待时间不能为负数。")
    targets = {path_key(path): canonical(path) for path in paths}
    paths = tuple(targets.values())
    _check_cancel(cancel)
    if not paths:
        return ()
    initial = _runtime(paths, config_path, cancel)
    _require_reliable(initial, paths)
    if expected_identities is not None:
        expected_identities = {path_key(path): identity for path, identity in expected_identities.items()}
        for path in paths:
            expected = expected_identities[path_key(path)]
            _require_vault_identity(path, expected)
            session = initial.session(path)
            if session is not None and session.directory_identity != expected:
                raise ObsidianLifecycleError(f"目标仓库目录身份已改变：{path}")
    closed = []
    for path in paths:
        _check_cancel(cancel)
        session = initial.session(path)
        if session is None:
            continue
        if progress:
            progress(Progress("closing_obsidian", f"正在关闭目标仓库 {path}…"))
        if _close_session(path, session, config_path, cancel, on_closed, graceful_timeout):
            closed.append(path)
        _check_cancel(cancel)
    assert_target_sessions_closed(paths, config_path=config_path)
    return tuple(closed)


def request_open_target(path, *, config_path=None, expected_identity=None):
    """Request reopening once, or return False for an already running target."""
    root = canonical(path)
    expected_identity = _require_vault_identity(root, expected_identity)
    runtime = _runtime((root,), config_path)
    _require_reliable(runtime, (root,))
    if runtime.session(root) is not None:
        return False
    uri = _verified_uri(root, config_path, expected_identity=expected_identity)
    runtime = _runtime((root,), config_path)
    _require_reliable(runtime, (root,))
    if runtime.session(root) is not None:
        return False
    _require_vault_identity(root, expected_identity)
    _open_uri(uri)
    return True


def _require_vault_identity(root, expected_identity):
    actual = vault_identity(root)
    if expected_identity is not None and actual != expected_identity:
        raise ObsidianLifecycleError(f"目标仓库目录身份已改变：{root}")
    return actual


def _verified_uri(root, config_path, *, expected_identity=None):
    catalog = discover_vaults((root,), recursive=False)
    if catalog.issues or len(catalog.vaults) != 1:
        detail = catalog.issues[0].message if catalog.issues else "目录已不再是有效仓库。"
        raise ObsidianLifecycleError(detail)
    vault_id = _registered_vault_id(root, config_path, expected_identity)
    _require_vault_identity(root, expected_identity)
    return "obsidian://open?vault=" + quote(vault_id, safe="")


def _open_uri(uri):
    if os.name != "nt":
        raise OSError("自动打开 Obsidian 仅支持 Windows。")
    os.startfile(uri)


def _registered_vault_id(root, config_path, root_identity):
    """Read a stable registry and require one exact path-to-ID association."""
    if config_path is None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ObsidianLifecycleError("无法定位 Obsidian 登记文件：APPDATA 未设置。")
        config_path = Path(appdata) / "obsidian" / "obsidian.json"
    config = canonical(config_path)
    assert_plain_chain(os.path.dirname(config))
    state = snapshot(config)
    if state is None:
        raise ObsidianLifecycleError("未找到 Obsidian 登记文件，请先在 Obsidian 中打开该仓库一次。")
    if state["kind"] != "file":
        raise ObsidianLifecycleError("Obsidian 登记文件必须是普通文件，不能是链接或特殊项。")
    try:
        payload = json.loads(_read_stable_file(config, state).decode("utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ObsidianLifecycleError(f"无法安全读取 Obsidian 登记文件：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("vaults"), dict):
        raise ObsidianLifecycleError("Obsidian 登记文件缺少有效的 vaults 对象。")
    matches = []
    registered_names = {}
    for vault_id, entry in payload["vaults"].items():
        value = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ObsidianLifecycleError("Obsidian 登记文件存在缺少有效路径的仓库记录。")
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not os.path.isabs(expanded):
            raise ObsidianLifecycleError("Obsidian 登记文件存在使用相对路径的仓库记录。")
        registered_names.setdefault(Path(canonical(expanded)).name.casefold(), set()).add(vault_id)
        matches_requested_path = path_key(expanded) == path_key(root)
        if not matches_requested_path:
            try:
                matches_requested_path = vault_identity(canonical(expanded)) == root_identity
            except (OSError, SyncError):
                # A different and currently inaccessible registered path
                # cannot supply an ID for this verified target directory.
                continue
        if matches_requested_path:
            # URI's vault parameter also accepts names. Reject malformed IDs
            # rather than accidentally opening another vault with that name.
            if re.fullmatch(r"[0-9a-fA-F]{16}", vault_id) is None:
                raise ObsidianLifecycleError("该仓库的 Obsidian 登记 ID 无效，请在 Obsidian 中重新登记仓库。")
            matches.append(vault_id)
    if not matches:
        raise ObsidianLifecycleError("仓库尚未登记到 Obsidian，请先在 Obsidian 中打开该仓库一次。")
    if len(matches) != 1:
        raise ObsidianLifecycleError("同一仓库路径对应多个 Obsidian 登记 ID，无法确定应打开哪一项。")
    if registered_names.get(matches[0].casefold(), set()) - {matches[0]}:
        raise ObsidianLifecycleError("目标的登记 ID 与另一仓库名称相同，无法安全请求打开。")
    return matches[0]
