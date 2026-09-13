"""Pure-Python repository-management services used by every ObManage page."""
from .catalog import discover_vaults, read_opened_vault_candidates
from .models import (
    FileTypeStatistics,
    ManagementIssue,
    OpenedVaultCandidates,
    VaultCatalogResult,
    VaultInfo,
    VaultStatistics,
    VaultStatisticsResult,
)
from .statistics import collect_vault_statistics

__all__ = [
    "FileTypeStatistics",
    "ManagementIssue",
    "OpenedVaultCandidates",
    "VaultCatalogResult",
    "VaultInfo",
    "VaultStatistics",
    "VaultStatisticsResult",
    "collect_vault_statistics",
    "discover_vaults",
    "read_opened_vault_candidates",
]
