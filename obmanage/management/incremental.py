"""Human-directed, per-vault synchronization between two collections (no Qt)."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from ..engine import SyncEngine
from ..models import Progress, SyncCancelled, SyncError, SyncPlan, SyncResult
from ..paths import (
    canonical, checked_child, revalidate_roots, snapshot,
    validate_roots, validate_state_separation,
)
from .catalog import discover_vaults
from .deployment import DeploymentEngine
from .trash import TrashCleanupEngine

_TASK_LOCK = threading.Lock()
ProgressCallback = Callable[[Progress], None] | None


def _cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("操作已取消。")


@dataclass(frozen=True)
class VaultPair:
    key: str
    relative_path: str
    local_path: str
    portable_path: str
    local_plan: SyncPlan | None = None
    portable_plan: SyncPlan | None = None
    errors: tuple[str, ...] = ()

    def plan_for(self, source: str) -> SyncPlan | None:
        if source == "local":
            return self.local_plan
        if source == "portable":
            return self.portable_plan
        raise SyncError("必须逐仓库明确选择电脑或移动硬盘作为来源。")


@dataclass(frozen=True)
class IncrementalAnalysis:
    local_root: str
    portable_root: str
    pairs: tuple[VaultPair, ...]
    identical_count: int = 0
    issues: tuple[str, ...] = ()
    context: dict = field(default_factory=dict, repr=False)
    duration_seconds: float = 0.0
    deep: bool = False


@dataclass(frozen=True)
class VaultOutcome:
    key: str
    source: str
    target: str
    result: SyncResult


@dataclass
class IncrementalResult:
    status: str = "failed"
    outcomes: list[VaultOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def selected_plans(analysis: IncrementalAnalysis, choices: Mapping[str, str]) -> list[tuple[VaultPair, SyncPlan]]:
    """Resolve only explicit choices and reject overlapping write/read scopes."""
    if analysis.issues:
        raise SyncError("扫描不完整，请先处理扫描问题后重新分析。")
    pairs = {pair.key: pair for pair in analysis.pairs}
    if not choices or not set(choices) <= pairs.keys():
        raise SyncError("请选择当前预览中的仓库来源。")
    selected: list[tuple[VaultPair, SyncPlan]] = []
    for key, source in choices.items():
        pair = pairs[key]
        plan = pair.plan_for(source)
        if pair.errors or plan is None or not plan.can_execute or not plan.has_changes:
            raise SyncError(f"{pair.relative_path}：该方向没有可执行的完整预览。")
        expected = ((pair.local_path, pair.portable_path) if source == "local"
                    else (pair.portable_path, pair.local_path))
        if (plan.source, plan.target) != expected or plan.mode != "mirror":
            raise SyncError("仓库来源或目标与预览不一致，请重新分析。")
        selected.append((pair, plan))
    scopes: list[tuple[str, str]] = []
    for pair, plan in selected:
        for path in (plan.source, plan.target):
            key = os.path.normcase(canonical(path))
            for previous, label in scopes:
                try:
                    common = os.path.commonpath((key, previous))
                except ValueError:
                    continue
                if common in (key, previous):
                    raise SyncError(
                        f"{pair.relative_path} 与 {label} 的仓库范围重叠；"
                        "父仓库已包含子仓库，请分批选择。"
                    )
            scopes.append((key, pair.relative_path))
    return selected


class IncrementalEngine:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)

    def analyze(self, local_root: str, portable_root: str, *, deep: bool = False,
                cancel: threading.Event | None = None,
                progress: ProgressCallback = None) -> IncrementalAnalysis:
        if not _TASK_LOCK.acquire(blocking=False):
            raise SyncError("已有仓库增量任务运行中。")
        try:
            started = time.monotonic()
            _cancelled(cancel)
            context = validate_roots(local_root, portable_root)
            if context["target_root"] is None:
                raise SyncError("请先选择已存在的移动硬盘仓库集合目录。")
            local_root, portable_root = context["source"], context["target"]
            validate_state_separation(self.state_dir, (local_root, portable_root))
            catalogs = [discover_vaults(root, cancel=cancel, progress=progress)
                        for root in (local_root, portable_root)]
            issues = [f"{issue.path or ''}：{issue.message}"
                      for catalog in catalogs for issue in catalog.issues]
            indexed = []
            for root, catalog in zip((local_root, portable_root), catalogs):
                paths: dict[str, tuple[str, str]] = {}
                for vault in catalog.vaults:
                    relative = os.path.relpath(vault.path, root).replace("\\", "/")
                    key = relative.casefold()
                    if key in paths:
                        issues.append(f"对应路径存在歧义：{relative}")
                    paths[key] = (relative, vault.path)
                indexed.append(paths)
            engine = SyncEngine(self.state_dir)
            pairs = []
            identical = 0
            keys = sorted(indexed[0].keys() | indexed[1].keys())
            for index, key in enumerate(keys):
                _cancelled(cancel)
                local, portable = indexed[0].get(key), indexed[1].get(key)
                relative = (local or portable)[0]
                local_path = local[1] if local else (
                    local_root if relative == "." else checked_child(local_root, relative))
                portable_path = portable[1] if portable else (
                    portable_root if relative == "." else checked_child(portable_root, relative))
                errors = []
                # An existing, unrecognized directory is not an authorized vault target.
                for entry, path in ((local, local_path), (portable, portable_path)):
                    if entry is None and snapshot(path) is not None:
                        errors.append(f"对应位置已存在但不是已发现的仓库：{path}")
                plans: dict[str, SyncPlan] = {}
                def report(event: Progress) -> None:
                    if progress:
                        progress(replace(event, phase="incremental_scan",
                                         message=f"{index + 1}/{len(keys)} · {relative} · {event.message}"))
                if not errors:
                    if local and portable:
                        plans["local"], plans["portable"] = engine.analyze_pair(
                            local_path, portable_path, deep=deep, cancel=cancel, progress=report)
                    else:
                        side, source, target = (("local", local_path, portable_path) if local
                                                else ("portable", portable_path, local_path))
                        plans[side] = engine.analyze(source, target, deep=deep,
                                                     cancel=cancel, progress=report)
                    for plan in plans.values():
                        errors.extend(plan.errors)
                pair = VaultPair(key, relative, local_path, portable_path,
                                 plans.get("local"), plans.get("portable"), tuple(errors))
                if errors or any(plan.has_changes for plan in plans.values()):
                    pairs.append(pair)
                else:
                    identical += 1
            revalidate_roots(context)
            return IncrementalAnalysis(local_root, portable_root, tuple(pairs), identical,
                                       tuple(issues), context, time.monotonic() - started, deep)
        finally:
            _TASK_LOCK.release()

    def _check_recovery(self) -> None:
        terminal = {"finalized", "rolled_back", "rolled_back_with_residuals",
                    "cancelled", "prepare_failed"}
        if any(batch.status not in terminal
               for batch in DeploymentEngine(self.state_dir).list_batches()):
            raise SyncError("存在待恢复部署事务，已阻止仓库增量写入。")
        if any(record.status != "finalized"
               for operation in TrashCleanupEngine(self.state_dir).list_operations()
               for record in operation.records):
            raise SyncError("存在待处理旧版回收站批次，已阻止仓库增量写入。")

    def execute(self, analysis: IncrementalAnalysis, choices: Mapping[str, str], *,
                allow_empty: bool = False, cancel: threading.Event | None = None,
                progress: ProgressCallback = None) -> IncrementalResult:
        result = IncrementalResult()
        if not _TASK_LOCK.acquire(blocking=False):
            result.errors.append("已有仓库增量任务运行中。")
            return result
        try:
            choices = dict(choices)
            selected = selected_plans(analysis, choices)
            if ((analysis.local_root, analysis.portable_root)
                    != (analysis.context.get("source"), analysis.context.get("target"))):
                raise SyncError("集合路径已改变，请重新分析。")
            validate_state_separation(self.state_dir, (analysis.local_root, analysis.portable_root))
            revalidate_roots(analysis.context)
            self._check_recovery()
            engine = SyncEngine(self.state_dir)
            # A stale later vault must reject the entire batch before the first write.
            for pair, plan in selected:
                _cancelled(cancel)
                if progress:
                    progress(Progress("preflight", f"执行前复核：{pair.relative_path}"))
                try:
                    engine.validate_plan(plan, cancel=cancel)
                except (OSError, SyncError, ValueError) as exc:
                    raise SyncError(f"执行前复核失败，仓库 {pair.relative_path}：{exc}") from exc
                if (plan.source_empty and any(item.action in {"delete", "rmdir"}
                                              for item in plan.items) and not allow_empty):
                    raise SyncError("来源为空，清空目标前需要额外确认。")
            total_bytes = sum(plan.bytes_to_copy for _, plan in selected)
            completed_bytes = 0
            reported_bytes = 0
            for index, (pair, plan) in enumerate(selected):
                _cancelled(cancel)
                revalidate_roots(analysis.context)
                self._check_recovery()

                def report(event: Progress) -> None:
                    nonlocal reported_bytes
                    if progress:
                        reported_bytes = max(reported_bytes, completed_bytes + min(
                            event.completed_bytes, plan.bytes_to_copy))
                        progress(replace(event, phase="incremental_execute",
                                         message=f"{index + 1}/{len(selected)} · {pair.relative_path} · {event.message}",
                                         completed_bytes=reported_bytes, total_bytes=total_bytes,
                                         completed_files=index, total_files=len(selected)))

                outcome = engine.execute(plan, allow_empty=allow_empty, cancel=cancel, progress=report)
                result.outcomes.append(VaultOutcome(pair.key, plan.source, plan.target, outcome))
                if outcome.status != "success":
                    result.status = outcome.status
                    result.errors.extend(f"{pair.relative_path}：{error}" for error in outcome.errors)
                    return result
                completed_bytes += outcome.copied_bytes
            result.status = "success"
            if progress:
                progress(Progress("done", "所选仓库更新完成", completed_bytes=completed_bytes,
                                  total_bytes=total_bytes, completed_files=len(selected),
                                  total_files=len(selected)))
        except SyncCancelled:
            result.status = "cancelled"
        except (OSError, SyncError, ValueError) as exc:
            result.errors.append(str(exc))
        finally:
            _TASK_LOCK.release()
        return result
