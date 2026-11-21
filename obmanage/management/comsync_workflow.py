"""Direct configuration sync, from a frozen selection through session recovery."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from ..models import Progress, SyncCancelled
from .comsync_targets import DirectoryBinding
from .deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentError,
    DeploymentPlan,
    DeploymentRequest,
    DeploymentResult,
    DeploymentSelection,
    DeploymentTarget,
)
from .obsidian_lifecycle import (
    assert_target_sessions_closed,
    close_target_sessions,
    request_open_target,
)
from .recovery import require_recovery_clear


@dataclass(frozen=True)
class ComSyncTarget:
    target_id: str
    binding: DirectoryBinding

    @property
    def path(self) -> str:
        return self.binding.path


@dataclass(frozen=True)
class ComSyncRequest:
    version: int
    source: str
    collection: DirectoryBinding
    selected_targets: tuple[ComSyncTarget, ...]
    component_names: tuple[str, ...]
    components: tuple[DeploymentComponent, ...]
    label: str
    open_after_sync: bool

    def deployment(self, targets: tuple[ComSyncTarget, ...]) -> DeploymentRequest:
        return DeploymentRequest(tuple(
            DeploymentSelection(component, DeploymentTarget(target.target_id, target.path))
            for component in self.components for target in targets
        ), label=self.label)


@dataclass(frozen=True)
class SyncApproval:
    request: ComSyncRequest
    plan: DeploymentPlan
    write_targets: tuple[str, ...]
    skipped: tuple[str, ...]
    closed_by_task: tuple[str, ...]


@dataclass(frozen=True)
class SessionOpening:
    target: str
    restore: bool
    status: str
    error: str = ""


@dataclass(frozen=True)
class ComSyncOutcome:
    status: str
    skipped: tuple[str, ...]
    execution: DeploymentResult | None
    error: str
    openings: tuple[SessionOpening, ...]


def _check_cancel(cancel) -> None:
    if cancel.is_set():
        raise SyncCancelled("本轮同步已取消。")


def _check_bindings(collection, source, targets) -> None:
    for binding in (collection, source, *(target.binding for target in targets)):
        if not binding.matches():
            raise DeploymentError(f"目录或所在磁盘的身份已改变：{binding.path}")
    for target in targets:
        if (not collection.contains(target.binding) or source.contains(target.binding)
                or target.binding.contains(source)):
            raise DeploymentError(f"目标与来源重叠或超出仓库集合：{target.path}")


def preview(state_dir, request: ComSyncRequest, cancel, progress) -> DeploymentPlan:
    source = DirectoryBinding.capture(request.source)
    return _precheck(DeploymentEngine(state_dir), request, source, cancel, progress)


def _precheck(engine, request, source, cancel, progress) -> DeploymentPlan:
    targets = request.selected_targets
    _check_cancel(cancel)
    _check_bindings(request.collection, source, targets)
    plan = engine.analyze(request.deployment(targets), cancel=cancel, progress=progress)
    _check_bindings(request.collection, source, targets)
    return plan


def _select_writes(targets, plan) -> tuple[ComSyncTarget, ...]:
    write_ids = {item.target_id for item in plan.targets if item.needs_deploy}
    return tuple(target for target in targets if target.target_id in write_ids)


def synchronize(state_dir, config_path, request: ComSyncRequest, cancel, progress,
                confirm: Callable[[SyncApproval], bool]) -> ComSyncOutcome:
    """Keep the close ledger alive through errors, cancellation and rollback."""
    if progress is None:
        progress = lambda _event: None
    selected = request.selected_targets
    closed: dict[str, ComSyncTarget] = {}
    skip_reasons: dict[str, str] = {}
    execution = None
    attempted_execution = False
    status = "failed"
    error = ""
    try:
        require_recovery_clear(state_dir)
        source = DirectoryBinding.capture(request.source)
        engine = DeploymentEngine(state_dir)
        before = _precheck(engine, request, source, cancel, progress)
        writes = _select_writes(selected, before)
        skip_reasons.update((target.path, "本轮预检查一致，已跳过")
                            for target in selected if target not in writes)
        if writes:
            _check_cancel(cancel)
            require_recovery_clear(state_dir)
            _check_bindings(request.collection, source, writes)
            paths = tuple(target.path for target in writes)
            by_path = {os.path.normcase(target.binding.physical): target for target in writes}
            by_path.update((os.path.normcase(target.path), target) for target in writes)

            def record_closed(path: str) -> None:
                target = by_path[os.path.normcase(path)]
                closed[target.path] = target

            progress(Progress("closing-sessions", "正在正常关闭需要写入的目标窗口…"))
            close_target_sessions(paths, config_path=config_path, cancel=cancel,
                                  progress=progress, on_closed=record_closed,
                                  expected_identities={target.path: target.binding.identity for target in writes})
            _check_cancel(cancel)
            _check_bindings(request.collection, source, writes)
            progress(Progress("final-analysis", "正在检查窗口关闭后保存的配置…"))
            plan = engine.analyze(request.deployment(writes), cancel=cancel,
                                  progress=progress, before_close=before)
            final_writes = _select_writes(writes, plan)
            skip_reasons.update((target.path, "窗口关闭后复核一致，已跳过")
                                for target in writes if target not in final_writes)
            if final_writes:
                final_paths = tuple(target.path for target in final_writes)
                approval = SyncApproval(
                    request, plan, final_paths, _skip_summary(skip_reasons), tuple(closed)
                )
                _check_bindings(request.collection, source, final_writes)
                if not confirm(approval):
                    raise SyncCancelled("已撤销本轮确认。")
                _check_cancel(cancel)
                require_recovery_clear(state_dir)

                def safety_check() -> None:
                    _check_bindings(request.collection, source, final_writes)
                    assert_target_sessions_closed(final_paths, config_path=config_path)

                attempted_execution = True
                execution = engine.execute(plan, cancel=cancel, progress=progress,
                                           safety_check=safety_check,
                                           rollback_safety_check=lambda paths: assert_target_sessions_closed(
                                               paths, config_path=config_path
                                           ))
        if execution is None:
            _check_cancel(cancel)
        status = execution.status if execution is not None else "no_changes"
    except SyncCancelled as exc:
        status, error = "cancelled", str(exc)
    except Exception as exc:
        error = str(exc)

    explicit_target = (
        selected[0] if len(selected) == 1 and request.open_after_sync
        and status in {"success", "no_changes"} and not cancel.is_set() else None
    )
    if closed or explicit_target is not None:
        progress(Progress("restoring-sessions", "正在检查并恢复本轮关闭的仓库窗口…"))
    blocked = (_recovery_problem(state_dir, execution, attempted_execution)
               if closed or explicit_target is not None else "")
    openings = [
        _open(target, config_path, restore=True, blocked=blocked)
        for target in closed.values()
    ]
    if explicit_target is not None and explicit_target.path not in closed:
        openings.append(_open(explicit_target, config_path, restore=False, blocked=blocked))
    return ComSyncOutcome(status, _skip_summary(skip_reasons), execution, error, tuple(openings))


def _skip_summary(reasons: dict[str, str]) -> tuple[str, ...]:
    return tuple(f"{path} · {reason}" for path, reason in reasons.items())


def _recovery_problem(state_dir, result, attempted_execution: bool) -> str:
    if result is not None and result.success:
        return "" if result.journal_status == "committed" else "无法核验同步事务记录。"
    if attempted_execution and result is None:
        return "执行结果无法核验，请先检查事务处理页。"
    try:
        require_recovery_clear(state_dir)
    except Exception as exc:
        return f"请先处理恢复事务：{exc}"
    return ""


def _open(target: ComSyncTarget, config_path, *, restore: bool,
          blocked: str = "") -> SessionOpening:
    if blocked:
        return SessionOpening(target.path, restore, "held", blocked)
    if not target.binding.matches():
        return SessionOpening(target.path, restore, "held", "目录身份已变化。")
    try:
        requested = request_open_target(
            target.path, config_path=config_path, expected_identity=target.binding.identity
        )
    except Exception as exc:
        return SessionOpening(target.path, restore, "failed", str(exc))
    return SessionOpening(target.path, restore, "requested" if requested else "already_running")
