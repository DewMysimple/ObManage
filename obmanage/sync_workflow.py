"""Shared preparation and execution for mirrored backups and full mirrors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from threading import Event

from .engine import SYNC_MODE_NO_VIDEO, SyncEngine
from .management.recovery import require_recovery_clear
from .models import Progress, SyncCancelled, SyncError, SyncPlan, SyncResult
from .paths import validate_root_pair, validate_roots


class SyncIntent(Enum):
    PREVIEW = auto()
    MANUAL = auto()
    SCHEDULED = auto()


@dataclass(frozen=True)
class SyncRequest:
    source: str
    target: str
    source_name: str
    target_name: str
    mode: str
    intent: SyncIntent
    deep: bool = False
    reviewed_plan: SyncPlan | None = field(default=None, repr=False, compare=False)


def sync_input_error(source: str, target: str) -> str:
    try:
        validate_root_pair(source, target)
    except (OSError, ValueError, SyncError) as exc:
        return str(exc)
    return ""


def would_empty_target(plan: SyncPlan) -> bool:
    return plan.source_empty and any(item.action in {"delete", "rmdir"} for item in plan.items)


def preview_copy(plan: SyncPlan) -> SyncPlan:
    """Give the table a separate view while the worker owns the executable plan."""
    return replace(plan, items=[replace(item) for item in plan.items],
                   errors=list(plan.errors), context={})


def run_sync(
    engine: SyncEngine,
    request: SyncRequest,
    cancel: Event,
    progress: Callable[[Progress], None],
    *,
    confirm: Callable[[SyncPlan], bool] | None = None,
    executing: Callable[[SyncPlan], None] | None = None,
) -> SyncPlan | SyncResult:
    plan = request.reviewed_plan
    if request.intent is not SyncIntent.PREVIEW and (
            plan is None or not plan.has_changes or would_empty_target(plan)):
        # Long analysis and a possible dialog each precede a second recovery check.
        # An already reviewed write goes directly to the gate just before execution.
        require_recovery_clear(engine.state_dir)
    if plan is None:
        plan = engine.analyze(request.source, request.target, deep=request.deep, mode=request.mode,
                              for_execution=request.intent is not SyncIntent.PREVIEW,
                              cancel=cancel, progress=progress)
    else:
        if (request.intent is not SyncIntent.MANUAL or request.deep or plan.mode != request.mode
                or not plan.context.get("validated") or not plan.can_execute):
            raise SyncError("预览与本轮同步不匹配，请重新预览变更。")
        # Resolve the configured paths again, including short-name aliases and link boundaries.
        roots = validate_roots(request.source, request.target)
        if roots["pair_id"] != plan.context.get("base_pair_id"):
            raise SyncError("预览后路径、方向或磁盘已改变，请重新预览变更。")
        if not plan.has_changes:
            engine.validate_plan(plan, cancel=cancel, progress=progress)
    if cancel.is_set():
        raise SyncCancelled()
    if request.intent is SyncIntent.PREVIEW or not plan.can_execute or not plan.has_changes:
        return plan
    allow_empty = would_empty_target(plan)
    if request.intent is SyncIntent.SCHEDULED and allow_empty:
        return plan
    if request.intent is SyncIntent.MANUAL and (request.reviewed_plan is None or allow_empty):
        if confirm is None or not confirm(plan):
            raise SyncCancelled()
    if cancel.is_set():
        raise SyncCancelled()
    require_recovery_clear(engine.state_dir)
    if executing is not None:
        executing(plan)
    return engine.execute(plan, allow_empty=allow_empty, cancel=cancel, progress=progress)


def confirmation_text(request: SyncRequest, plan: SyncPlan) -> tuple[str, str, str, str]:
    """Format the exact direction, paths and effects of the prepared plan."""
    backup = request.mode == SYNC_MODE_NO_VIDEO
    empty = would_empty_target(plan)
    if empty:
        title = "排除视频后来源为空" if backup else "来源仓库为空"
        approve = "确认清空非视频内容" if backup else "确认清空目标"
        warning = (
            "来源没有可管理的非视频内容。继续将清空目标中本次可管理的非视频内容。"
            if backup else "来源为空。继续将清空目标仓库的内容。"
        )
    else:
        title = "确认本轮仓库备份" if backup else "确认本轮仓库镜像"
        approve = "确认备份" if backup else "确认同步"
        warning = ""
    counts = plan.counts
    scope = "视频保持原样。" if backup else ""
    summary = (
        f"{request.source_name} → {request.target_name} · 1 个目标\n"
        f"新增 {counts['add'] + counts['mkdir']:,} 项 · "
        f"更新 {counts['update'] + counts['rename']:,} 项 · "
        f"删除 {counts['delete'] + counts['rmdir']:,} 项\n"
        f"来源只读取。{scope}本轮保持 Obsidian 窗口原状。"
    )
    if warning:
        summary = f"{warning}\n\n{summary}"
    details = f"来源 · {request.source_name}\n{plan.source}\n\n目标 · {request.target_name}\n{plan.target}"
    return title, summary, details, approve
