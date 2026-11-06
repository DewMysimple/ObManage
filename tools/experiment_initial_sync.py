"""One-use, initially empty, Windows collection copy with independent audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import uuid4

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from obmanage.app import acquire_instance_lock
from obmanage import __version__
from obmanage.engine import SyncEngine, _hash_file
from obmanage.file_types import is_video_filename
from obmanage.management.catalog import _read_stable_file
from obmanage.management.incremental import IncrementalAnalysis, IncrementalEngine, selected_plans
from obmanage.management.recovery import require_recovery_clear
from obmanage.models import SyncCancelled, SyncError
from obmanage.paths import (assert_plain_chain, hold_windows_directory, identity, native, snapshot,
                            validate_state_separation)
from obmanage.reading import ReadScope
from obmanage.scanning.metrics import ScanMetrics
from obmanage.settings import default_state_dir
from tools.benchmark_readonly_paths import BoundaryError, Protection, system_temp


ROOT = Path(__file__).resolve().parents[1]
MIN_MARGIN = 4 * 1024**3
MAX_PREVIEW_AGE = 600
UNSAFE_ATTRIBUTES = 0x40 | 0x400 | 0x1000 | 0x40000 | 0x400000


class ExperimentError(SyncError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def cancelled(event):
    if event.is_set():
        raise SyncCancelled("操作已取消。")


def _units(value):
    return len(value.encode("utf-16-le")) // 2


def inventory(root: Path, cancel: Event, *, status: str) -> dict[str, dict]:
    """Independent raw namespace, including hidden and application-reserved entries."""
    entries = {}
    pending = [(root, "")]
    try:
        while pending:
            cancelled(cancel)
            folder, relative = pending.pop()
            before = assert_plain_chain(folder, attributes_only_ancestors=True)
            if before is None or before["kind"] != "dir":
                raise ExperimentError(status, "目录已消失或不再是普通目录。")
            if relative in entries and entries[relative] != before:
                raise ExperimentError(status, "目录在清单枚举期间改变。")
            entries[relative] = before
            names = set()
            with os.scandir(native(folder)) as iterator:
                for entry in iterator:
                    cancelled(cancel)
                    name = entry.name
                    attributes = entry.stat(follow_symlinks=False).st_file_attributes
                    child = folder / name
                    key = f"{relative}/{name}" if relative else name
                    if (attributes & UNSAFE_ATTRIBUTES or os.path.isreserved(name)
                            or _units(name) > 255 or _units(native(child)) > 32700
                            or name.casefold() in names):
                        raise ExperimentError(status, "发现链接、占位、非法名称或大小写冲突。")
                    names.add(name.casefold())
                    state = snapshot(child)
                    if state is None or state["kind"] not in ("dir", "file"):
                        raise ExperimentError(status, "发现不可读、不稳定或非普通条目。")
                    entries[key] = state
                    if state["kind"] == "dir":
                        if name.casefold().startswith(".obmanage-deploy-"):
                            raise ExperimentError(status, "发现保留的部署恢复目录。")
                        pending.append((child, key))
            if snapshot(folder) != before:
                raise ExperimentError(status, "目录在清单枚举期间改变。")
    except OSError as exc:
        raise ExperimentError(status, "无法完整枚举文件或目录。") from exc
    return entries


def ancestors(relative):
    while "/" in relative:
        relative = relative.rsplit("/", 1)[0]
        yield relative


def within(relative, scope):
    return relative == scope or relative.startswith(scope + "/")


def projection(source, scopes):
    """Union of disjoint vaults plus the directories needed to reach them."""
    directories = {p for scope in scopes for p in ancestors(scope)}
    return {path: state for path, state in source.items() if
            path in directories or any(within(path, scope) for scope in scopes)}


def totals(entries):
    files = [state["size"] for state in entries.values() if state["kind"] == "file"]
    return dict(files=len(files), directories=len(entries) - len(files),
                bytes=sum(files), largest_file=max(files, default=0))


def shape(entries):
    return {path: (state["kind"], state["size"]) for path, state in entries.items() if path}


def changes(before, after):
    counts = Counter()
    for path in before.keys() | after.keys():
        first, second = before.get(path), after.get(path)
        if first != second:
            counts[(second or first)["kind"]] += 1
            counts["inserted" if first is None else "removed" if second is None else "modified"] += 1
    return dict(counts)


def audit(source, target, expected, cancel, report, progress=None):
    """New raw observations and full SHA-256 reads on each side."""
    started = time.perf_counter()
    origin = inventory(source, cancel, status="INVALID_SOURCE_CHANGED")
    destination = inventory(target, cancel, status="INVALID_TARGET_CHANGED")
    intended = shape(expected)
    found = shape(destination)
    record = dict(source_bytes=0, target_bytes=0, source_seconds=0.0, target_seconds=0.0,
                  missing=len(intended.keys() - found.keys()), extra=len(found.keys() - intended.keys()),
                  mismatch=0, sha256_files=0)
    report.setdefault("audits", []).append(record)
    try:
        if any(origin.get(path) != state for path, state in expected.items()):
            raise ExperimentError("INVALID_SOURCE_CHANGED", "核验时来源快照已变化。")
        record["mismatch"] = sum(intended[p] != found[p] for p in intended.keys() & found.keys())
        if record["missing"] or record["extra"] or record["mismatch"]:
            raise ExperimentError("MISMATCH", "目标文件或目录清单存在缺失、多余或大小差异。")
        digests = {}
        metadata = Counter()
        read_scopes = {"source": ReadScope(str(source)), "target": ReadScope(str(target))}
        for path in sorted(intended):
            if origin[path]["kind"] != "file":
                continue
            if origin[path]["mtime_ns"] != destination[path]["mtime_ns"]:
                metadata["mtime_differs"] += 1
            values = []
            for role, entries in (("source", origin), ("target", destination)):
                started_file = time.perf_counter()
                try:
                    safe_path, state = read_scopes[role].observe(path)
                    if state != entries[path]:
                        raise SyncError("文件快照已改变。")
                    values.append(_hash_file(safe_path, state, cancel, None, path))
                except (OSError, SyncError) as exc:
                    status = "INVALID_SOURCE_CHANGED" if role == "source" else "INVALID_TARGET_CHANGED"
                    raise ExperimentError(status, "核验时文件无法稳定读取。") from exc
                record[f"{role}_seconds"] += time.perf_counter() - started_file
                record[f"{role}_bytes"] += entries[path]["size"]
            record["sha256_files"] += 1
            if progress:
                progress(record["sha256_files"], record["source_bytes"] + record["target_bytes"])
            if values[0] != values[1]:
                record["mismatch"] += 1
            digests[path] = values[0]
        if inventory(source, cancel, status="INVALID_SOURCE_CHANGED") != origin:
            raise ExperimentError("INVALID_SOURCE_CHANGED", "独立核验期间来源变化。")
        if inventory(target, cancel, status="INVALID_TARGET_CHANGED") != destination:
            raise ExperimentError("INVALID_TARGET_CHANGED", "独立核验期间目标变化。")
        if record["mismatch"]:
            raise ExperimentError("MISMATCH", "独立 SHA-256 发现内容不一致。")
        record["metadata"] = dict(metadata)
        record["verified"] = True
        return destination, digests
    finally:
        record["seconds"] = time.perf_counter() - started


@dataclass(frozen=True)
class Preview:
    id: str
    kind: str
    created: float
    analysis: IncrementalAnalysis
    scopes: tuple[str, ...]
    target_entries: dict

    @property
    def phrase(self):
        return f"{self.kind}-{self.id}"


class InitialSync:
    def __init__(self, source, target, work, recovery, protection, cancel, report, emit):
        self.source, self.target, self.work = source, target, work
        self.recovery, self.protection, self.cancel = recovery, protection, cancel
        self.report, self.emit = report, emit
        self.engine = IncrementalEngine(work / "state")
        self.missing = snapshot(target) is None
        self.target_binding = None if self.missing else identity(snapshot(target))
        self.source_entries = None
        self.scopes = ()
        self.completed = ()
        self.ledger = None
        self.pending = None
        self.application_lock = None

    def check(self):
        cancelled(self.cancel)
        if self.application_lock is not None:
            owned, pid, _, _ = self.application_lock.getLockInfo()
            if not owned or pid != os.getpid():
                raise ExperimentError("BLOCKED", "应用任务锁已失去，停止新增。")
        for role, status in (("local", "INVALID_SOURCE_CHANGED"), ("portable", "INVALID_TARGET_CHANGED")):
            try:
                self.protection.inputs[role].verify()
            except (OSError, SyncError, BoundaryError) as exc:
                raise ExperimentError(status, "卷或绑定目录身份已变。") from exc
        if identity(snapshot(self.target)) != self.target_binding:
            raise ExperimentError("INVALID_TARGET_CHANGED", "目标集合身份已变。")
        require_recovery_clear(self.recovery)

    def source_inventory(self):
        result = inventory(self.source, self.cancel, status="INVALID_SOURCE_CHANGED")
        if self.source_entries is not None and result != self.source_entries:
            self.report["source_drift"] = changes(self.source_entries, result)
            raise ExperimentError("INVALID_SOURCE_CHANGED", "来源较首次预览已变化。")
        return result

    def target_inventory(self):
        if self.target_binding is None:
            return {}
        return inventory(self.target, self.cancel, status="INVALID_TARGET_CHANGED")

    def require_space(self, need):
        reserve = max(MIN_MARGIN, need["bytes"] // 10) + need["largest_file"]
        free = shutil.disk_usage(native(self.target.parent)).free
        self.report["space"] = dict(free=free, required=need["bytes"] + reserve, reserve=reserve)
        if free < need["bytes"] + reserve:
            raise ExperimentError("BLOCKED", "移动盘剩余空间低于本轮安全门槛。")

    def analyze(self, root, label):
        metrics = ScanMetrics(label)
        with metrics.activate():
            result = self.engine.analyze(str(self.source), str(root), cancel=self.cancel)
        self.report.setdefault("scans", []).append(metrics.report())
        if result.issues or any(pair.errors for pair in result.pairs):
            raise ExperimentError("BLOCKED", "原生增量预览不完整或发现异常。")
        return result

    def _audit(self, scopes):
        self.check()
        last = 0.0

        def progress(files, size):
            nonlocal last
            now = time.monotonic()
            if now - last >= 15:
                self.emit(f"独立 SHA-256 已核验 {files:,} 个文件，读取 {size:,} 字节")
                last = now

        result = audit(self.source, self.target, projection(self.source_entries, scopes),
                       self.cancel, self.report, progress)
        self.check()
        return result

    def preview(self, kind):
        self.pending = None
        self.check()
        started = time.perf_counter()
        target_before = self.target_inventory()
        if self.ledger is None:
            if set(target_before) - {""}:
                self.report["target_original"] = "nonempty"
                self.report["target_conflicts"] = totals({p: s for p, s in target_before.items() if p})
                raise ExperimentError("BLOCKED", "目标集合非空，发现已有文件或目录。")
            if self.report["target_original"] == "present":
                self.report["target_original"] = "empty"
        else:
            live, digests = self._audit(self.completed)
            if (live, digests) != self.ledger:
                raise ExperimentError("INVALID_TARGET_CHANGED", "已完成内容与本次执行账本不一致。")
            target_before = live
        source_before = self.source_inventory()
        root = self.work / "empty-preview" if self.missing else self.target
        analysis = self.analyze(root, "initial_preview" if not self.scopes else "remaining_preview")
        source_after = self.source_inventory()
        if source_after != source_before:
            self.report["source_drift"] = changes(source_before, source_after)
            raise ExperimentError("INVALID_SOURCE_CHANGED", "生成预览期间来源清单出现变化。")
        if self.target_inventory() != target_before:
            raise ExperimentError("INVALID_TARGET_CHANGED", "生成预览期间目标清单出现变化。")
        self.check()
        if not self.scopes:
            vaults = sorted(pair.relative_path for pair in analysis.pairs)
            scopes = tuple(path for path in vaults if not any(p in vaults for p in ancestors(path)))
            if not scopes or "." in scopes:
                raise ExperimentError("BLOCKED", "来源必须是含独立子仓库的集合。")
            covered = projection(source_before, scopes)
            covered_dirs = {p.casefold() for p, s in covered.items() if s["kind"] == "dir"}
            if any((path + "/.obsidian").casefold() not in covered_dirs for path in vaults):
                raise ExperimentError("BLOCKED", "存在无法由父仓库完整覆盖的嵌套仓库。")
            if any(_units(native(self.target / path)) > 32700 for path in covered):
                raise ExperimentError("BLOCKED", "目标路径超过安全长路径范围。")
            gaps = {p: s for p, s in source_before.items() if p and p not in covered}
            self.report.update(discovered=len(vaults), top_level=len(scopes),
                               nested=len(vaults) - len(scopes), coverage_gap=totals(gaps),
                               projected=totals(covered), videos=sum(is_video_filename(p)
                               for p, s in covered.items() if s["kind"] == "file"),
                               longest_target_path=max(_units(str(self.target / p)) for p in covered))
            self.scopes, self.source_entries = scopes, source_before
        remaining = tuple(scope for scope in self.scopes if scope not in self.completed)
        self.require_space(totals(projection(source_before, remaining)))
        self._selected(analysis, remaining, root)
        if kind == "CANARY":
            sizes = {scope: totals(projection(source_before, (scope,))) for scope in remaining}
            remaining = (min(remaining, key=lambda scope: (sizes[scope]["bytes"], sizes[scope]["files"])),)
        preview = Preview(uuid4().hex[:12], kind, time.monotonic(), analysis, remaining, target_before)
        summary = totals(projection(source_before, remaining))
        self.report["preview"] = dict(id=preview.id, kind=kind, selected=len(remaining), **summary,
                                      seconds=time.perf_counter() - started,
                                      deletes=0, updates=0, renames=0)
        self.pending = preview
        return preview

    def _selected(self, analysis, scopes, root):
        choices = {path.casefold(): "local" for path in scopes}
        pairs = selected_plans(analysis, choices)
        for pair, plan in pairs:
            relative = pair.relative_path
            if Path(plan.source) != self.source / relative or Path(plan.target) != root / relative:
                raise ExperimentError("BLOCKED", "仓库来源或目标越出当前预览。")
            SyncEngine.require_new_target(plan)
            prefix = relative + "/"
            live = {p[len(prefix):]: s for p, s in self.source_entries.items() if p.startswith(prefix)}
            if (plan.context["source_entries"] != live
                    or identity(plan.context["source_root"]) != identity(self.source_entries[relative])):
                raise ExperimentError("INVALID_SOURCE_CHANGED", "原生计划与独立来源清单不同。")
        return choices, pairs

    def _authorize(self, preview, response):
        current, self.pending = self.pending, None
        if (preview is not current or response != preview.phrase
                or time.monotonic() - preview.created > MAX_PREVIEW_AGE):
            raise ExperimentError("BLOCKED", "现场确认不匹配、已使用或已超时。")
        self.check()
        self.source_inventory()
        if self.target_inventory() != preview.target_entries:
            raise ExperimentError("INVALID_TARGET_CHANGED", "确认后目标清单已变化。")

    def create_root(self, preview, response, pinned):
        self._authorize(preview, response)
        if not self.missing or preview.kind != "CREATE":
            raise ExperimentError("BLOCKED", "本轮没有新建集合根的授权。")
        self.require_space(totals(projection(self.source_entries, preview.scopes)))
        self.target_binding = pinned.enter_context(hold_windows_directory(self.target, create=True))
        self.missing = False
        self.report["target_root_created"] = True
        self.check()

    def run_batch(self, preview, response):
        self._authorize(preview, response)
        if self.missing or preview.kind not in ("CANARY", "COPY"):
            raise ExperimentError("BLOCKED", "真实目标缺失或本轮未授权复制。")
        choices, pairs = self._selected(preview.analysis, preview.scopes, self.target)
        self.require_space(totals(projection(self.source_entries, preview.scopes)))
        native_engine = SyncEngine(self.engine.state_dir)
        preflight = ScanMetrics("batch_preflight")
        with preflight.activate():
            for _, plan in pairs:
                native_engine.validate_plan(plan, cancel=self.cancel)
        self.report.setdefault("execution", []).append(preflight.report())
        self.check()
        if self.target_inventory() != preview.target_entries:
            raise ExperimentError("INVALID_TARGET_CHANGED", "整批预复核期间目标变化。")
        before = preview.target_entries
        scaffolds = set()
        self.report["copy_authorized"] = True
        for index, (pair, plan) in enumerate(pairs, 1):
            self.check()
            if self.target_inventory() != before:
                raise ExperimentError("INVALID_TARGET_CHANGED", "执行下一个仓库前发现目标变化。")
            vault_started = time.monotonic()
            self.emit(f"新增仓库 {index}/{len(pairs)}，本仓库 {plan.bytes_to_copy:,} 字节")
            with ExitStack() as parents:
                for parent in reversed(tuple(ancestors(pair.relative_path))):
                    path = self.target / parent
                    state = before.get(parent)
                    if state is None:
                        parents.enter_context(hold_windows_directory(path, create=True))
                        scaffolds.add(parent)
                    else:
                        parents.enter_context(hold_windows_directory(path, identity(state)))
                metrics = ScanMetrics("copy_vault")
                last = 0.0

                def progress(event):
                    nonlocal last
                    now = time.monotonic()
                    if now - last >= 15:
                        self.emit(f"仓库 {index}/{len(pairs)} · {event.relative_path or '复核'} · "
                                  f"已处理 {event.completed_bytes:,}/{plan.bytes_to_copy:,} 字节")
                        last = now

                with metrics.activate():
                    result = self.engine.execute(preview.analysis, {pair.key: choices[pair.key]},
                                                 create_only=True, cancel=self.cancel, progress=progress)
                record = dict(metrics.report(), ordinal=self.scopes.index(pair.relative_path) + 1,
                              seconds_elapsed=time.monotonic() - vault_started, status=result.status,
                              copied_files=sum(o.result.copied_files for o in result.outcomes),
                              copied_bytes=sum(o.result.copied_bytes for o in result.outcomes),
                              deleted=sum(o.result.deleted_files + o.result.deleted_dirs for o in result.outcomes),
                              error_count=len(result.errors))
                self.report.setdefault("vaults", []).append(record)
                if result.status != "success" or record["deleted"]:
                    raise ExperimentError("PARTIAL", f"原生执行停止，仓库序号 {record['ordinal']}，状态 {result.status}。")
                current = self.target_inventory()
                new = projection(self.source_entries, (pair.relative_path,))
                if (shape(current) != (shape(before) | shape(new))
                        or any((identity(current.get(path)) != identity(state) if path in scaffolds
                                or path in set(ancestors(pair.relative_path)) | {""}
                                else current.get(path) != state) for path, state in before.items())):
                    raise ExperimentError("INVALID_TARGET_CHANGED", "本仓库发布后目标出现预期外变化。")
                before = current
            self.completed += (pair.relative_path,)
        self.source_inventory()
        self.ledger = self._audit(self.completed)
        self.report["completed"] = len(self.completed)

    def rescan(self):
        self.check()
        result = self.analyze(self.target, "no_change")
        self.check()
        self.source_inventory()
        if self.target_inventory() != self.ledger[0]:
            raise ExperimentError("INVALID_TARGET_CHANGED", "无变化复扫期间目标发生变化。")
        self.report["rescan"] = dict(identical=result.identical_count, changed=len(result.pairs))
        return result


def _load_allowlist(path, temp):
    """Exactly one operator-provided pair, stored outside both data volumes."""
    path = Path(path)
    if not path.is_absolute() or path.parent != temp:
        raise BoundaryError("白名单文件须直接位于已核验的 Windows 临时目录。")
    state = snapshot(path)
    if not state or state["kind"] != "file" or state["size"] > 8192:
        raise BoundaryError("无法安全读取白名单。")
    values = json.loads(_read_stable_file(str(path), state).decode("utf-8-sig"))
    if (not isinstance(values, dict) or set(values) != {"local", "portable"}
            or any(not isinstance(value, str) for value in values.values())):
        raise BoundaryError("白名单必须只包含 local 和 portable 两个绝对路径。")
    local, portable = Path(values["local"]), Path(values["portable"])
    if (not local.is_absolute() or not portable.is_absolute()
            or ".." in local.parts or ".." in portable.parts
            or local.drive.upper() != "E:" or portable.drive.upper() != "H:"
            or len(local.parts) != 2 or len(portable.parts) != 3 or local.name != portable.name):
        raise BoundaryError("只接受本轮固定方向和集合层级。")
    return local, portable


def _operator_confirm(experiment, preview, report_path):
    print(json.dumps(experiment.report["preview"], ensure_ascii=False), flush=True)
    print(f"来源（只读） {experiment.source}\n目标（仅新增） {experiment.target}", flush=True)
    print(f"私密报告 {report_path}", flush=True)
    print(json.dumps({k: experiment.report[k] for k in
                     ("sha", "version", "space", "coverage_gap")}, ensure_ascii=False), flush=True)
    return input(f"确认本次预览请输入 {preview.phrase}。其他输入退出。\n").strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", required=True, type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="只读预览（默认）")
    modes.add_argument("--execute", action="store_true", help="每批另需当下交互确认")
    args = parser.parse_args(argv)
    if args.execute and not sys.stdin.isatty():
        parser.error("真实执行需要交互终端。")
    cancel = Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    temp = system_temp()
    source, target = _load_allowlist(args.allowlist, temp)
    protection = Protection(source, target.parent, temp)
    if (source != protection.inputs["local"].physical or target.parent != protection.inputs["portable"].physical
            or protection.temp.binding[0] in {d.binding[0] for d in protection.inputs.values()}
            or protection.inputs["local"].binding[0] == protection.inputs["portable"].binding[0]
            or any(not d.binding[0].startswith("\\\\?\\volume{") for d in protection.inputs.values())):
        raise BoundaryError("卷身份不唯一，或白名单路径不是已核验的物理路径。")
    recovery = Path(validate_state_separation(default_state_dir(), (str(source), str(target.parent))))
    if recovery.drive != temp.drive:
        raise BoundaryError("应用状态必须位于本机系统卷。")
    implementation = hashlib.sha256()
    for relative in ("tools/experiment_initial_sync.py", "obmanage/paths.py", "obmanage/engine.py",
                     "obmanage/copying.py", "obmanage/management/incremental.py"):
        implementation.update((ROOT / relative).read_bytes())
    report = dict(id=uuid4().hex, source=str(source), target=str(target), version=__version__,
                  implementation_sha256=implementation.hexdigest(),
                  source_binding=protection.inputs["local"].binding,
                  target_parent_binding=protection.inputs["portable"].binding,
                  sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  target_original="missing" if snapshot(target) is None else "present",
                  status="BLOCKED", copy_authorized=False, target_root_created=False,
                  execute_requested=args.execute)
    with ExitStack() as resources:
        resources.enter_context(protection.temp.hold())
        work = temp / f"obmanage-initial-{report['id']}"
        os.mkdir(native(work), mode=0o700)
        work_binding = protection.outside(work)
        resources.enter_context(work_binding.hold())
        (work / "empty-preview").mkdir()
        report_path = work / "report.json"
        experiment = InitialSync(source, target, work, recovery, protection, cancel, report,
                                 lambda message: print(message, flush=True))

        def save():
            work_binding.verify()
            stage = work / (uuid4().hex + ".json")
            with open(native(stage), "x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(native(stage), native(report_path))

        try:
            # Guard before the first scratch database and never follow aliases.
            experiment.check()
            preview = experiment.preview("CREATE" if experiment.missing else "CANARY")
            report["status"] = "DRY_RUN"
            save()
            print(f"只读预览 {report_path}", flush=True)
            print(json.dumps({key: report[key] for key in
                             ("target_original", "discovered", "top_level", "nested",
                              "coverage_gap", "projected", "space", "preview")}, ensure_ascii=False), flush=True)
            if args.execute:
                from PySide6.QtCore import QCoreApplication

                application = QCoreApplication.instance() or QCoreApplication([sys.executable])
                if snapshot(recovery) is None:
                    recovery.mkdir(mode=0o700)
                resources.enter_context(protection.outside(recovery).hold())
                lock = acquire_instance_lock(recovery)
                if lock is None:
                    raise ExperimentError("BLOCKED", "ObManage 已占用应用任务锁。")
                resources.callback(lock.unlock)
                experiment.application_lock = lock
                experiment.check()
                resources.enter_context(protection.inputs["portable"].hold())
                if experiment.missing:
                    experiment.create_root(preview, _operator_confirm(experiment, preview, report_path), resources)
                    preview = experiment.preview("CANARY")
                else:
                    resources.enter_context(hold_windows_directory(target, experiment.target_binding))
                save()
                experiment.run_batch(preview, _operator_confirm(experiment, preview, report_path))
                save()
                if set(experiment.completed) != set(experiment.scopes):
                    preview = experiment.preview("COPY")
                    save()
                    experiment.run_batch(preview, _operator_confirm(experiment, preview, report_path))
                experiment.rescan()
                report["status"] = ("COVERAGE_GAP" if any(report["coverage_gap"].values()) else "VERIFIED")
        except (OSError, ValueError, SyncError, SyncCancelled, EOFError) as exc:
            report["status"] = getattr(exc, "status", "PARTIAL" if report["copy_authorized"] else "BLOCKED")
            report["error_type"] = type(exc).__name__
            report["error"] = str(exc) if isinstance(exc, ExperimentError) else "安全检查、访问或交互失败。"
            print(f"{report['status']} · {report['error']}", flush=True)
        finally:
            save()
        print(f"{report['status']} · {report_path}", flush=True)
        return 0 if report["status"] in ("DRY_RUN", "VERIFIED", "COVERAGE_GAP") else 1


if __name__ == "__main__":
    raise SystemExit(main())
