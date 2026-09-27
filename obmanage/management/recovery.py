"""Read-only recovery gate for tasks that create new persistent output."""
from ..models import SyncError
from .deployment import DeploymentEngine
from .trash import TrashCleanupEngine


def require_recovery_clear(state_dir):
    terminal = {"finalized", "rolled_back", "rolled_back_with_residuals", "cancelled", "prepare_failed"}
    if any(batch.status not in terminal for batch in DeploymentEngine(state_dir).list_batches()):
        raise SyncError("存在待恢复部署事务，已阻止新的写入。")
    if any(record.status != "finalized" for operation in TrashCleanupEngine(state_dir).list_operations()
           for record in operation.records):
        raise SyncError("存在待处理旧版回收站批次，已阻止新的写入。")
