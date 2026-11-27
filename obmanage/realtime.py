"""Local-to-portable synchronization of saved changes. No Qt or background scheduler."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field

from .engine import (SYNC_MODE_MIRROR, SYNC_MODE_NO_VIDEO, SyncEngine, _cancelled, _hash_file,
                     _key, _mutate_target_file, _profile_pair_id, _protected_directory_keys,
                     _RESERVED_DIRECTORY_PREFIXES, _scan, _WINDOWS_DELETE_READONLY_ERRORS)
from .file_types import is_video_filename
from .management.recovery import require_recovery_clear
from .models import PlanItem, SyncCancelled, SyncError, SyncPlan, SyncResult
from .paths import (canonical, checked_child_snapshot, identity, native, overlapping_roots,
                    revalidate_roots, snapshot, validate_root_pair, validate_roots, validate_state_separation)
from .realtime_store import Agreed, LiveCopyRecords, RealtimeStore, path_key
from .realtime_watch import require_closed_writer
from .store import BaselineStore


DELETE_DELAY = 6.0


def _check_binding(roots):
    revalidate_roots(roots)
    for side, birth in roots.get("root_birth", {}).items():
        if side not in ("source", "target") or os.lstat(native(roots[side])).st_birthtime_ns != birth:
            raise SyncError("绑定的仓库根目录已改变，请核对磁盘与目录。")


def _mount_paths(guid):
    try:
        return os.listmounts(guid.replace("volume{", "Volume{", 1))
    except (OSError, ValueError):
        return []


def _portable_locator(roots):
    guid = roots["target_volume"].rsplit(":", 2)[0]
    if os.name == "nt" and guid.startswith("\\\\?\\volume{"):
        path = roots["target"]
        for mount in sorted(_mount_paths(guid), key=len, reverse=True):
            if path_key(path).startswith(path_key(mount).rstrip("/") + "/"):
                return (guid, os.path.relpath(path, mount))
    return None


def portable_path(configured, locator):
    """Resolve only the chosen volume and original path within it. Every write rechecks its identity."""
    if not locator:
        return configured
    guid, relative = locator
    if (not isinstance(guid, str) or not guid.startswith("\\\\?\\volume{")
            or os.path.isabs(relative) or os.path.splitdrive(relative)[0]
            or any(part in ("", ".", "..") for part in relative.replace("\\", "/").split("/"))):
        raise SyncError("实时绑定的卷内路径无效，请重新绑定。")
    mounts = _mount_paths(guid)
    candidates = [canonical(os.path.join(m, relative)) for m in mounts]
    return next((p for p in candidates if path_key(p) == path_key(configured)),
                candidates[0] if candidates else None)


def automatic_overlap(source, target, other_source, other_target):
    def overlaps(left, right):
        paths = [os.path.normcase(canonical(os.path.realpath(native(p)))) for p in (left, right)]
        return overlapping_roots(paths) is not None
    return (overlaps(target, other_target) or overlaps(target, other_source)
            or overlaps(source, other_target))


def large_removal(count, total):
    return count >= 100 or (count >= 10 and count * 4 >= total)


def _require_disjoint(source, target, automatic):
    for other_source, other_target, locator in automatic:
        destination = portable_path(other_target, locator) or other_target
        if automatic_overlap(source, target, other_source, destination):
            raise SyncError("自动同步的读写范围重叠，请暂停其中一项。")


def enable_realtime(state_dir, source, target, mode, cancel, progress, confirm, *, automatic=()):
    """Confirm the initial differences, then seed a private checkpoint from verified bytes."""
    validate_state_separation(state_dir, (source, target))
    _require_disjoint(source, target, automatic)
    require_recovery_clear(state_dir)
    engine = SyncEngine(state_dir)
    plan = engine.analyze(source, target, mode=mode, deep=True, cancel=cancel, progress=progress)
    if not plan.can_execute:
        raise SyncError("\n".join(plan.errors))
    if not confirm(plan):
        raise SyncCancelled("已取消开启实时同步。")
    _cancelled(cancel)
    require_recovery_clear(state_dir)
    _require_disjoint(source, target, automatic)
    if plan.has_changes or plan.context["target_root"] is None:
        # This is the user's explicit, itemized first synchronization, including an empty source.
        result = engine.execute(plan, allow_empty=plan.source_empty, cancel=cancel, progress=progress)
        if result.status == "cancelled":
            raise SyncCancelled()
        if result.status != "success":
            raise SyncError("\n".join(result.errors))
        expected_root = result.target_root
    else:
        engine.validate_plan(plan, cancel=cancel)
        expected_root = plan.context["target_root"]
    roots = validate_roots(source, target)
    roots["configured_paths"] = [canonical(source), canonical(target)]
    roots["portable_locator"] = _portable_locator(roots)
    if os.name == "nt":
        roots["root_birth"] = {s: os.lstat(native(roots[s])).st_birthtime_ns for s in ("source", "target")}
    revalidate_roots(plan.context, target_root=expected_root)
    if identity(roots["target_root"]) != identity(expected_root):
        raise SyncError("建立实时记录时目标目录已被替换。")
    with closing(BaselineStore(state_dir)) as cache, closing(RealtimeStore(state_dir)) as store:
        verified = {path_key(name): row for name, row in cache.records(plan.pair_id).items()}
        protected = plan.context["protected_target_directories"]
        with store.db:
            token = store.bind(mode, roots)
            for name, src in plan.context["source_entries"].items():
                _cancelled(cancel)
                if src["kind"] == "file":
                    row = verified.get(path_key(name))
                    if not row or row[0] != src:
                        raise SyncError(f"建立实时记录时来源快照已改变：{name}")
                    _, b, digest = row
                else:
                    _, b = checked_child_snapshot(roots["target"], name)
                    if not b or b["kind"] != "dir":
                        raise SyncError(f"建立实时记录时目标目录已改变：{name}")
                    digest = ""
                target_name = name
                if src["kind"] == "dir" and _key(name) in protected:
                    target_name = _spelling(roots["target"], name)
                store.save(token, Agreed(name, target_name, src, b, digest))
        return token


class _NeedInventory(SyncError):
    """A notification can contain an 8.3 alias. Enumerate the actual names."""


def _spelling(root, name):
    path, state = checked_child_snapshot(root, name)
    if state is None:
        return name
    resolved = canonical(os.path.realpath(native(path), strict=True))
    actual = os.path.relpath(resolved, root).replace("\\", "/")
    if path_key(name) != path_key(actual):
        raise _NeedInventory(f"路径解析后超出观察范围或使用了别名：{name}")
    return actual


def _minimal_scopes(names):
    kept = {}
    for name in sorted(names, key=lambda n: n.count("/")):
        key = path_key(name)
        parent = key
        while parent and parent not in kept:
            parent = parent.rpartition("/")[0]
        if not parent:
            kept[key] = name
    return tuple(kept.values())


def _expand_scope(roots, name, store, token, parents):
    parts = name.split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if parent not in parents:
            _, src = checked_child_snapshot(roots["source"], parent)
            _, dst = checked_child_snapshot(roots["target"], parent)
            before = store.target_directory(token, parent)
            parents[parent] = (not src or not dst or src["kind"] != "dir" or identity(dst) != identity(before)
                               or _spelling(roots["source"], parent) != _spelling(roots["target"], parent))
        if parents[parent]:
            return parent
    return name


def _inventory(root, scopes, mode, cancel, progress=None):
    if scopes is None:
        return _scan(root, cancel, progress, mode=mode)
    entries, excluded = {}, {}
    for name in scopes:
        _cancelled(cancel)
        path, state = checked_child_snapshot(root, name)
        if state is None:
            continue
        name = _spelling(root, name)
        parts = name.casefold().split("/")
        dirs = parts if state["kind"] == "dir" else parts[:-1]
        if any(part.startswith(_RESERVED_DIRECTORY_PREFIXES) for part in dirs):
            continue
        if mode == SYNC_MODE_NO_VIDEO and state["kind"] == "file" and is_video_filename(name):
            excluded[name] = state
            continue
        entries[name] = state
        if state["kind"] == "dir":
            children, videos = _scan(path, cancel, progress, mode=mode)
            entries.update((name + "/" + n, s) for n, s in children.items())
            excluded.update((name + "/" + n, s) for n, s in videos.items())
    return entries, excluded


@dataclass
class LiveResult:
    copies: SyncResult = field(default_factory=lambda: SyncResult("success"))
    issues: dict[str, str] = field(default_factory=dict)
    retry: set[str] = field(default_factory=set)
    covered: tuple[str, ...] | None = None
    waiting_disk: bool = False
    namespace_checked: bool = False
    created_dirs: int = 0

    @property
    def changed(self):
        r = self.copies
        return bool(r.copied_files or r.deleted_dirs or r.deleted_files or r.renamed_items or self.created_dirs)


def _observe_pair(roots, names, store, token, mode, cancel, progress, out):
    source, target = roots["source"], roots["target"]
    if names is not None:
        valid, parents = [], {}
        try:
            for name in names:
                try:
                    valid.append(_expand_scope(roots, name, store, token, parents))
                except _NeedInventory:
                    raise
                except (OSError, SyncError) as exc:
                    out.issues[name] = str(exc)
                    out.retry.add(name)
            src, dst, excluded, scopes = {}, {}, {}, []
            for name in _minimal_scopes(valid):
                try:
                    a, _ = _inventory(source, (name,), mode, cancel)
                    b, videos = _inventory(target, (name,), mode, cancel)
                except _NeedInventory:
                    raise
                except (OSError, SyncError) as exc:
                    out.issues[name] = str(exc)
                    out.retry.add(name)
                    continue
                scopes.append(name)
                src.update(a)
                dst.update(b)
                excluded.update(videos)
            return src, dst, excluded, tuple(scopes)
        except _NeedInventory:
            pass
    src, _ = _inventory(source, None, mode, cancel, progress)
    dst, excluded = _inventory(target, None, mode, cancel, progress)
    return src, dst, excluded, None


def reconcile(state_dir, source, target, mode, token, names, cancel, progress=None, *, idle=False, automatic=()):
    """None requests a full content check. Named saves inspect only the affected paths."""
    if mode not in (SYNC_MODE_MIRROR, SYNC_MODE_NO_VIDEO):
        raise SyncError("实时同步模式无效。")
    validate_state_separation(state_dir, (source, target))
    require_recovery_clear(state_dir)
    engine = SyncEngine(state_dir)
    if not engine._lock.acquire(blocking=False):
        raise SyncError("已有同步任务运行中。")
    try:
        with closing(RealtimeStore(state_dir)) as store:
            roots = store.roots(token, mode)
            configured = roots["configured_paths"]
            if ([path_key(canonical(p)) for p in (source, target)] != [path_key(p) for p in configured]
                    or path_key(canonical(os.path.realpath(native(source)))) != path_key(roots["source"])):
                raise SyncError("实时同步的位置与已确认绑定不同。")
            source = roots["source"]
            out = LiveResult()
            target = portable_path(roots["target"], roots.get("portable_locator"))
            if target is None or not os.path.isdir(native(target)):
                out.waiting_disk = True
                out.issues[""] = "目标目录不可用，等待原移动硬盘和仓库目录。"
                return out
            validate_root_pair(source, target)
            _require_disjoint(source, target, automatic)
            validate_state_separation(state_dir, (source, target))
            roots.update(target=target, target_anchor=target, target_anchor_state=roots["target_root"])
            _check_binding(roots)
            src, dst, excluded, scopes = _observe_pair(roots, names, store, token, mode, cancel, progress, out)
            src_keys = {path_key(n) for n in src}
            protected = _protected_directory_keys(excluded)
            # A disappearance may be half of a move. Discover all destinations first.
            # Reuse metadata only for unrelated, previously verified pairs in this expansion.
            expanded = scopes is not None and any(
                path_key(n) not in src_keys and _key(n) not in protected for n in dst)
            requested = scopes
            if expanded:
                src, dst, excluded, scopes = _observe_pair(roots, None, store, token, mode, cancel, progress, out)
            out.namespace_checked = scopes is None
            out.covered = requested if expanded else () if idle else scopes
            before = store.records(token, scopes)
            cache = LiveCopyRecords(store, token, target)
            # None forces hashing. An empty tuple allows reuse across the entire idle scan.
            reuse_except = requested if expanded else () if idle else None
            try:
                _apply_changes(engine, store, cache, token, roots, mode, src, dst, excluded,
                               before, reuse_except, out, cancel, progress)
                _check_binding(roots)
            except (OSError, SyncError, SyncCancelled, sqlite3.Error) as exc:
                if not out.changed:
                    raise
                out.issues[""] = f"本轮部分完成，已停止后续操作。{exc}"
                out.retry.add("")
            return out
    finally:
        engine._lock.release()


def _digest(root, name, state, cancel):
    if not state or state["kind"] == "dir":
        return ""
    path, _ = checked_child_snapshot(root, name)
    return _hash_file(path, state, cancel, None, name)


def _below(key, parent):
    return key == parent or key.startswith(parent + "/")


def _in_scopes(key, scope_keys):
    while key:
        if key in scope_keys:
            return True
        key = key.rpartition("/")[0]
    return False


def _moved_destinations(removals, before, sources, src, digests, expected):
    """Recognize a move by a unique still-live source object with the agreed bytes."""
    live = {}
    for key, name in sources.items():
        ident = identity(src[name])
        if ident[-1] and _key(name) in expected:
            live[ident] = None if ident in live else key
    moved = {}
    for item in removals:
        old = before.get(path_key(item.relative_path))
        if old is None:
            continue
        key = live.get(identity(old.source))
        if key is None:
            continue
        digest = digests.get(key, before[key].digest if key in before else None)
        if digest == old.digest:
            moved[item.relative_path] = (sources[key], digest)
    return moved


@dataclass
class _Changes:
    writes: list[PlanItem] = field(default_factory=list)
    renames: list[PlanItem] = field(default_factory=list)
    removals: list[PlanItem] = field(default_factory=list)
    source_hashes: dict[str, str] = field(default_factory=dict)
    target_hashes: dict[str, str] = field(default_factory=dict)
    blocked_dirs: set[str] = field(default_factory=set)

    def blocked(self, name):
        return _in_scopes(path_key(name), self.blocked_dirs)


def _compare(source, target, src, dst, sources, targets, before, protected, staged_keys,
             reuse_except, store, token, out, cancel):
    changes = _Changes()
    base_changes, forgotten = [], []
    forced = {path_key(n) for n in reuse_except or ()}

    def issue(name, message, directory=False):
        out.issues[name] = str(message)
        if directory:
            changes.blocked_dirs.add(path_key(name))

    for key in sorted(sources.keys() | targets.keys() | before.keys()):
        _cancelled(cancel)
        if key in staged_keys:
            continue
        source_name, target_name = sources.get(key), targets.get(key)
        source_state, target_state, old = src.get(source_name), dst.get(target_name), before.get(key)
        name = source_name or target_name or old.source_name
        if not source_state and not target_state:
            forgotten.append(key)
            continue
        if not source_state and target_state["kind"] == "dir" and _key(target_name) in protected:
            forgotten.append(key)
            continue
        directory = any(state and state["kind"] == "dir" for state in (source_state, target_state))
        if source_state and target_state and source_state["kind"] != target_state["kind"]:
            issue(name, "文件与目录类型不同，请选择手动处理方向。", True)
            continue
        name_diff = bool(source_name and target_name and source_name.rsplit("/", 1)[-1] != target_name.rsplit("/", 1)[-1])
        keep_case = directory and _key(name) in protected
        if (target_state and old and directory and identity(target_state) != identity(old.target)):
            issue(name, "硬盘目录已被替换，请核对。", True)
            continue
        # Idle checks and move discovery can reuse unaffected pairs. An explicit
        # check, reconnect, or dirty file always compares actual bytes.
        if (reuse_except is not None and old and source_state == old.source and target_state == old.target
                and source_name == old.source_name and target_name == old.target_name
                and not _in_scopes(key, forced)):
            continue
        try:
            source_digest = _digest(source, source_name, source_state, cancel)
            target_digest = _digest(target, target_name, target_state, cancel)
        except (OSError, SyncError) as exc:
            issue(name, exc, directory)
            out.retry.add(name)
            continue
        changes.source_hashes[key], changes.target_hashes[key] = source_digest, target_digest
        if source_state and target_state and source_digest == target_digest and (not name_diff or keep_case):
            agreed = Agreed(source_name, target_name, source_state, target_state, source_digest)
            if old != agreed:
                base_changes.append(agreed)
            continue
        # Require the previous agreed destination before an automatic overwrite/removal.
        if ((old is None and target_state is not None) or (old and (not target_state or old.digest != target_digest
                or old.target["kind"] != target_state["kind"]
                or old.target_name.rsplit("/", 1)[-1] != target_name.rsplit("/", 1)[-1]))):
            issue(name, "硬盘内容或名称独立改变，请先带回本机，或手动按选定方向同步。", directory)
            continue
        if not source_state:
            changes.removals.append(PlanItem("rmdir" if directory else "delete", target_name))
        else:
            if name_diff and not keep_case:
                changes.renames.append(PlanItem("rename", source_name))
            if not target_state or source_digest != target_digest:
                action = "mkdir" if directory else "update" if target_state else "add"
                changes.writes.append(PlanItem(action, source_name, source_state["size"]))

    with store.db:
        restored = store.held(token).intersection(sources)
        store.db.executemany("DELETE FROM held_removals WHERE token=? AND path=?", ((token, k) for k in restored))
        store.reset_removals(token, sources.keys() | targets.keys() | before.keys(),
                             {path_key(i.relative_path) for i in changes.removals}, out.covered is None)
        for row in base_changes:
            if not changes.blocked(row.source_name):
                store.save(token, row)
        for key in forgotten:
            store.forget(token, key)

    return changes


def _apply_changes(engine, store, cache, token, roots, mode, src, dst, excluded,
                   before, reuse_except, out, cancel, progress):
    source, target = roots["source"], roots["target"]
    plan = SyncPlan(source, target, mode=mode, pair_id=_profile_pair_id(roots["pair_id"], mode))
    plan.context = dict(roots, source_entries=src, target_entries=dst,
                        target_names={_key(n): n for n in dst})
    sources = {path_key(n): n for n in src}
    targets = {path_key(n): n for n in dst}
    protected = _protected_directory_keys(excluded)
    expected = {_key(n): state for n, state in dst.items()}
    # A crashed staging file is recoverable only through the engine's exact ownership record.
    owned = dict(cache.temps(plan.pair_id))
    staged = []
    for path, ident in owned.items():
        try:
            name = os.path.relpath(canonical(path), target).replace("\\", "/")
        except ValueError:
            continue
        n = targets.get(path_key(name))
        if (n and path_key(n) not in sources and identity(dst[n]) == ident
                and os.path.basename(path).startswith(".obmanage-") and path.endswith(".tmp")):
            staged.append(PlanItem("delete", n))
    changes = _compare(source, target, src, dst, sources, targets, before, protected,
                       {path_key(i.relative_path) for i in staged}, reuse_except, store, token, out, cancel)

    def guard(item, *, publishing=False):
        _cancelled(cancel)
        _check_binding(roots)
        n = item.relative_path
        k = path_key(n)
        parent = n.rpartition("/")[0]
        while parent:
            key = path_key(parent)
            target_parent = expected.get(_key(parent)) or store.target_directory(token, parent)
            _, current = checked_child_snapshot(target, parent)
            if not target_parent or identity(current) != identity(target_parent):
                raise SyncError(f"硬盘父目录身份已改变：{parent}")
            source_parent = src.get(sources.get(key))
            if source_parent and identity(checked_child_snapshot(source, parent)[1]) != identity(source_parent):
                raise SyncError(f"本机父目录身份已改变：{parent}")
            parent = parent.rpartition("/")[0]
        source_path, actual = checked_child_snapshot(source, n)
        source_state = src.get(sources.get(k))
        if actual != source_state:
            raise SyncError(f"来源发生变化，等待重新核对：{n}")
        if actual and actual["kind"] == "file":
            require_closed_writer(source_path)
            if publishing and _digest(source, n, actual, cancel) != changes.source_hashes[k]:
                raise SyncError(f"来源内容在提交前改变：{n}")
        path, actual = checked_child_snapshot(target, n)
        want = expected.get(_key(n))
        if ((identity(actual) != identity(want)) if want and want["kind"] == "dir" else actual != want):
            raise SyncError(f"硬盘项目在执行前改变：{n}")
        if actual and actual["kind"] == "file":
            require_closed_writer(path)
            if _digest(target, n, actual, cancel) != changes.target_hashes[k]:
                raise SyncError(f"硬盘文件内容在执行前改变：{n}")
        return path, actual

    require_recovery_clear(engine.state_dir)
    for item in sorted(changes.renames, key=lambda i: i.relative_path.count("/")) + sorted(
            changes.writes, key=lambda i: (i.action != "mkdir", i.relative_path.count("/"))):
        n = item.relative_path
        k = path_key(n)
        if changes.blocked(n):
            continue
        try:
            path, actual = guard(item)
            if item.action == "rename":
                if actual["kind"] == "dir":
                    if any(_below(path_key(p), k) for p in out.issues):
                        raise SyncError("目录内有待处理内容，暂缓改名。")
                    live, videos = _inventory(target, (n,), mode, cancel)
                    original = {p: v for p, v in dst.items() if _below(path_key(p), k)}
                    if live != original or videos:
                        raise SyncError("改名前目录清单已改变。")
                engine._rename(plan, item, expected, roots["target_root"], cancel, progress, out.copies)
                state = expected[_key(n)]
                # A rename may also require a subsequent content update.
                digest = changes.target_hashes[k]
            elif item.action == "mkdir":
                os.mkdir(native(path))
                out.created_dirs += 1
                state = checked_child_snapshot(target, n)[1]
                if not state or state["kind"] != "dir":
                    raise SyncError("创建的目录已被替换。")
                digest = ""
            else:
                if actual is not None:
                    dst[plan.context["target_names"][_key(n)]] = actual
                state = engine.copy_observed_file(plan, item, cache, cancel, progress, out.copies,
                                                  lambda: guard(item, publishing=True), changes.source_hashes[k])
                digest = changes.source_hashes[k]
            expected[_key(n)] = state
            with store.db:
                store.save(token, Agreed(n, _spelling(target, n), src[n], state, digest))
        except (OSError, SyncError) as exc:
            out.issues[n] = str(exc)
            if src[n]["kind"] == "dir":
                changes.blocked_dirs.add(k)
            out.retry.add(n)

    if staged and not out.issues:
        remaining = engine._recover_temps(plan, cache, staged, expected, out.copies, cancel, progress,
                                          list(owned.items()))
        for item in remaining:
            out.issues[item.relative_path] = "暂存文件在核对期间发生变化，已保留。"
    if not changes.removals:
        return
    moved = _moved_destinations(changes.removals, before, sources, src, changes.source_hashes, expected)
    unpaired = len(changes.removals) - len(moved)
    empty = not src or (any(i.action == "delete" for i in changes.removals)
                        and not any(s["kind"] == "file" for s in src.values()))
    now = time.time()
    with store.db:
        recent = store.recent_deletions(token, now)
        keys = {path_key(i.relative_path) for i in changes.removals}
        mass = unpaired and large_removal(unpaired + recent, len(dst) + recent)
        if empty or mass:
            store.hold(token, keys)
        held = store.held(token).intersection(keys)
    if held:
        reason = "有效来源为空" if empty else "本次删除数量异常" if mass else "此前暂停的删除仍待处理"
        out.issues[""] = f"{reason}，暂停删除 {len(changes.removals)} 项。请预览后手动同步。"
        for item in changes.removals:
            out.issues[item.relative_path] = "等待手动确认删除范围"
        return
    if out.issues:
        out.issues.setdefault("", "本轮有项目未完成，已保留待删除内容。")
        if out.retry:
            out.retry.update(i.relative_path for i in changes.removals)
        return
    # Confirm a complete source observation before propagation of a move/deletion.
    # Directory timestamps may have changed; the source has not been written by us.
    require_recovery_clear(engine.state_dir)
    live, _ = _inventory(source, None, mode, cancel)
    if live != src:
        out.retry.update(i.relative_path for i in changes.removals)
        return
    for item in sorted(changes.removals, key=lambda i: (i.action == "rmdir", -i.relative_path.count("/"))):
        n = item.relative_path
        k = path_key(n)
        try:
            with store.db:
                mature = store.mature_removal(token, n, [identity(dst[n]), changes.target_hashes[k]], now, DELETE_DELAY)
            if not mature:
                out.retry.add(n)
                continue
            if item.action == "rmdir" and any(_below(path_key(p), k) for p in out.retry):
                out.retry.add(n)
                continue
            if n in moved:
                other, digest = moved[n]
                _, new_state = checked_child_snapshot(target, other)
                if (identity(new_state) != identity(expected[_key(other)])
                        or _digest(target, other, new_state, cancel) != digest):
                    raise SyncError(f"移动后的目标仍待核对，已保留原位置：{other}")
            path, state = guard(item)
            if item.action == "delete":
                _mutate_target_file(path, state, "待删除文件已改变", lambda: os.unlink(native(path)),
                                    _WINDOWS_DELETE_READONLY_ERRORS)
                out.copies.deleted_files += 1
            else:
                os.rmdir(native(path))
                out.copies.deleted_dirs += 1
            with store.db:
                store.forget(token, k)
                if n not in moved:
                    store.db.execute("INSERT INTO recent_deletes VALUES (?,?)", (token, time.time()))
            if (checked_child_snapshot(source, n)[1] is not None
                    or checked_child_snapshot(target, n)[1] is not None):
                raise SyncError(f"删除后路径再次变化，等待核对：{n}")
        except (OSError, SyncError) as exc:
            out.issues[n] = str(exc)
            out.retry.update(i.relative_path for i in changes.removals)
            break
