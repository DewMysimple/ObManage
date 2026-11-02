"""Read-only preparation and explicit bulk removal of transaction copies/records."""
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from .deployment import DeploymentEngine, RecoveryPreview, TERMINAL_BATCH_STATUSES
from .trash import TrashCleanupEngine, TrashClearPreview
from ..models import SyncCancelled, SyncError


@dataclass(frozen=True)
class TransactionClearPreview:
    deployments: tuple[RecoveryPreview, ...]
    quarantines: tuple[TrashClearPreview, ...]
    pending_deployments: int

    @property
    def count(self) -> int:
        return len(self.deployments) + len(self.quarantines)

    @property
    def details(self) -> str:
        sections = [f"部署事务 {p.batch_id}\n" + "\n".join(p.details)
                    for p in self.deployments]
        sections.extend(f"隔离事务 {p.operation.operation_id}\n" + "\n".join(
            f"仓库：{r.vault_root}\n副本：{r.backup_path}" for r in p.operation.records)
                        for p in self.quarantines)
        return "\n\n".join(sections)


@dataclass(frozen=True)
class TransactionCompletionPreview:
    clear_preview: TransactionClearPreview
    selected: RecoveryPreview | None = None

    @property
    def count(self) -> int:
        return self.clear_preview.count

    @property
    def details(self) -> str:
        return self.clear_preview.details


@dataclass(frozen=True)
class TransactionClearResult:
    removed: int
    total: int
    errors: tuple[str, ...]
    cancelled: bool = False

    @property
    def success(self) -> bool:
        return self.removed == self.total and not self.errors and not self.cancelled


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise SyncCancelled("清除已取消。")


def inspect_transaction_clear(state_dir: Path, cancel: Event) -> TransactionClearPreview:
    """Freeze the complete eligible set before the one destructive confirmation."""
    return _inspect_transaction_clear(state_dir, cancel, allow_cleanup_errors=False)


def _inspect_transaction_clear(state_dir: Path, cancel: Event, *,
                               allow_cleanup_errors: bool) -> TransactionClearPreview:
    deployment, trash = DeploymentEngine(state_dir), TrashCleanupEngine(state_dir)
    batches, operations = deployment.list_batches(), trash.list_operations()
    previews = []
    pending = 0
    for batch in batches:
        _check_cancel(cancel)
        if batch.status not in TERMINAL_BATCH_STATUSES:
            pending += 1
            continue
        preview = deployment.inspect_recovery(batch.batch_id)
        if preview.cleanup_error and not allow_cleanup_errors:
            raise SyncError(f"事务 {batch.batch_id[:8]} 无法清理：{preview.cleanup_error}")
        previews.append(preview)
    quarantines = []
    for operation in operations:
        _check_cancel(cancel)
        quarantines.append(trash.inspect_clear(operation.operation_id, cancel=cancel))
    _check_cancel(cancel)
    return TransactionClearPreview(tuple(previews), tuple(quarantines), pending)


def inspect_transaction_completion(
        state_dir: Path, cancel: Event,
        selected_deployment: RecoveryPreview | None = None) -> TransactionCompletionPreview:
    """Freeze cleanup consent, optionally including one pending deployment."""
    _check_cancel(cancel)
    deployment = DeploymentEngine(state_dir)
    if selected_deployment is not None:
        selected_batch = deployment.get_batch(selected_deployment.batch_id)
        if selected_batch.revision != selected_deployment.revision:
            raise SyncError("所选事务在检查后已改变，请重新检查并确认。")
    # An ended batch with unreadable/changed copies must not stop a different
    # selected batch from ending. Its authenticated preview stays in the set,
    # and clear_transaction will retain its copies/record and report the error.
    # Journal authentication and quarantine inspection failures still raise.
    clear_preview = _inspect_transaction_clear(state_dir, cancel, allow_cleanup_errors=True)
    if selected_deployment is None:
        return TransactionCompletionPreview(clear_preview)
    _check_cancel(cancel)
    selected_batch = deployment.get_batch(selected_deployment.batch_id)
    if selected_batch.revision != selected_deployment.revision:
        raise SyncError("所选事务在检查后已改变，请重新检查并确认。")
    if selected_batch.status in TERMINAL_BATCH_STATUSES:
        return TransactionCompletionPreview(clear_preview)
    # A pending batch can be ended even when its copies are unsafe to remove.
    # Keep that cleanup error in the frozen preview so execution retains the
    # ended record and all unverified bytes instead of broadening consent.
    clear_preview = replace(
        clear_preview,
        deployments=(selected_deployment, *clear_preview.deployments),
        pending_deployments=clear_preview.pending_deployments - 1,
    )
    return TransactionCompletionPreview(clear_preview, selected_deployment)


def complete_transactions(state_dir: Path, preview: TransactionCompletionPreview,
                          cancel: Event) -> TransactionClearResult:
    """End the selected batch and remove only the copies/records confirmed."""
    if cancel.is_set():
        return TransactionClearResult(0, preview.count, (), True)
    clear_preview, selected = preview.clear_preview, preview.selected
    if selected is not None:
        deployment = DeploymentEngine(state_dir)
        try:
            batch = deployment.get_batch(selected.batch_id)
            if (batch.revision != selected.revision
                    or batch.status in TERMINAL_BATCH_STATUSES):
                raise SyncError("所选事务在检查后已改变，请重新检查并确认。")
            if cancel.is_set():
                return TransactionClearResult(0, preview.count, (), True)
            result = deployment.resolve(selected.batch_id, expected_revision=selected.revision)
        except (OSError, ValueError, SyncError) as exc:
            return TransactionClearResult(0, preview.count, (str(exc),))
        if not result.success:
            return TransactionClearResult(
                0, preview.count, result.errors or ("所选事务未能结束。",)
            )
        # resolve performs exactly one authenticated journal update. Preserve
        # every observed tree and require this one revision transition; never
        # re-inspect/adopt a later state after the user's confirmation.
        clear_preview = replace(clear_preview, deployments=tuple(
            replace(item, revision=selected.revision + 1)
            if item.batch_id == selected.batch_id else item
            for item in clear_preview.deployments
        ))
    return clear_transactions(state_dir, clear_preview, cancel)


def clear_transactions(state_dir: Path, preview: TransactionClearPreview,
                       cancel: Event) -> TransactionClearResult:
    """Only clear the confirmed set; newly created transactions are never adopted."""
    deployment, trash = DeploymentEngine(state_dir), TrashCleanupEngine(state_dir)
    removed, errors = 0, []
    for item in (*preview.deployments, *preview.quarantines):
        if cancel.is_set():
            return TransactionClearResult(removed, preview.count, tuple(errors), True)
        if isinstance(item, RecoveryPreview):
            result = deployment.clear_transaction(item.batch_id, preview=item)
            success, failures = result.success, result.errors
            identifier = item.batch_id
        else:
            identifier = item.operation.operation_id
            result = trash.clear_transaction(identifier, cancel=cancel, expected_revision=item.revision)
            success = result.status == "success"
            failures = tuple(f.message for f in result.failures)
        if success:
            removed += 1
        else:
            errors.append(f"{identifier[:8]}：" + ("；".join(failures) or "清除未完成"))
    return TransactionClearResult(removed, preview.count, tuple(errors), cancel.is_set())
