"""Component scope and creation-time selection for the unified workbench."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ..paths import assert_plain_chain, native
from ..models import SyncCancelled, SyncError
from .deployment import DeploymentComponent
from .models import ManagementIssue, VaultCatalogResult, VaultInfo


def source_root(value: str) -> Path:
    """Accept the old direct component pickers as well as a vault/suite root."""
    path = Path(value)
    if path.name.casefold() in {".obsidian", ".claude", ".claudian", "file"}:
        return path.parent
    if path.name.casefold() == "templater":
        return path.parent.parent if path.parent.name.casefold() == "file" else path.parent
    return path


def components(value: str, selected) -> tuple[DeploymentComponent, ...]:
    root = source_root(value)
    chosen = set(selected)
    result = [DeploymentComponent.direct(key, root / ("." + key), "." + key)
              for key in ("claude", "claudian", "obsidian") if key in chosen]
    if chosen & {"templater", "file"}:
        result.append(DeploymentComponent.templater(root))
    if "file" in chosen:
        result.extend(DeploymentComponent.directory("file_" + name.lower(), root, "File/" + name)
                      for name in ("Note", "Attachment"))
    return tuple(result)


@dataclass(frozen=True)
class DatedVault(VaultInfo):
    created_ns: int | None = None


def with_creation_times(catalog: VaultCatalogResult, cancel=None) -> VaultCatalogResult:
    """Read real birth time; never substitute modification time on other systems."""
    rows, issues = [], list(catalog.issues)
    for vault in catalog.vaults:
        if cancel is not None and cancel.is_set():
            raise SyncCancelled("已取消读取仓库创建时间。")
        created = None
        try:
            assert_plain_chain(vault.path)
            state = os.stat(native(vault.path), follow_symlinks=False)
            if not stat.S_ISDIR(state.st_mode):
                raise ValueError("仓库根已变成非目录")
            created = getattr(state, "st_birthtime_ns", None)
            if created is None and os.name == "nt":
                created = state.st_ctime_ns
            if created is None:
                raise ValueError("文件系统未提供创建时间")
        except (OSError, ValueError, SyncError) as exc:
            issues.append(ManagementIssue("creation_time_unavailable",
                f"无法读取仓库创建时间：{exc}", vault.path, "warning"))
        rows.append(DatedVault(vault.path, vault.name, vault.parent_path, created))
    return VaultCatalogResult(tuple(rows), tuple(issues))


def newest_vaults(vaults) -> tuple[VaultInfo, ...]:
    rows = tuple(vaults)
    if not rows or any(getattr(row, "created_ns", None) is None for row in rows):
        return ()
    newest = max(row.created_ns for row in rows)
    return tuple(row for row in rows if row.created_ns == newest)
