"""Read-only preparation and explicit bulk removal of transaction copies/records."""
from dataclasses import dataclass
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
        if preview.cleanup_error:
            raise SyncError(f"事务 {batch.batch_id[:8]} 无法清理：{preview.cleanup_error}")
        previews.append(preview)
    quarantines = []
    for operation in operations:
        _check_cancel(cancel)
        quarantines.append(trash.inspect_clear(operation.operation_id, cancel=cancel))
    _check_cancel(cancel)
    return TransactionClearPreview(tuple(previews), tuple(quarantines), pending)


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
