"""Read-only collection scope and identity bindings for .comSync targets."""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..models import SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, identity, native, volume_identity
from .catalog import discover_vaults
from .comsync import with_creation_times
from .models import ManagementIssue, VaultCatalogResult


def _path_key(path: str) -> str:
    return os.path.normcase(path)


@dataclass(frozen=True)
class DirectoryBinding:
    path: str
    physical: str
    identity: tuple[str, int, int]
    volume: str

    @classmethod
    def capture(cls, path: str) -> DirectoryBinding:
        path = canonical(path)
        state = assert_plain_chain(path)
        if state is None or state["kind"] != "dir":
            raise SyncError(f"目录不可用：{path}")
        physical = canonical(os.path.realpath(native(path)))
        binding = identity(state)
        # Some Windows filesystems return inode zero at the volume root.
        if not state["inode"] and os.path.dirname(physical) != physical:
            raise SyncError(f"无法核验目录身份：{path}")
        volume = volume_identity(physical)
        if (identity(assert_plain_chain(physical)) != binding
                or identity(assert_plain_chain(path)) != binding):
            raise SyncError(f"目录在检查期间已改变：{path}")
        return cls(path, physical, binding, volume)

    def matches(self) -> bool:
        try:
            current = self.capture(self.path)
        except (OSError, SyncError):
            return False
        return current == self

    def contains(self, other: DirectoryBinding) -> bool:
        root = _path_key(self.physical)
        try:
            return os.path.commonpath((root, _path_key(other.physical))) == root
        except ValueError:
            return False


@dataclass(frozen=True)
class TargetCatalog:
    root: str
    root_binding: DirectoryBinding | None
    catalog: VaultCatalogResult
    bindings: tuple[DirectoryBinding, ...] = ()


def scan_target_catalog(root: str, cancel, progress) -> TargetCatalog:
    try:
        root_binding = DirectoryBinding.capture(root)
    except (OSError, SyncError) as exc:
        return TargetCatalog(root, None, VaultCatalogResult(issues=(
            ManagementIssue("collection_unavailable", str(exc), root),
        )))

    result = with_creation_times(
        discover_vaults(root_binding.physical, cancel=cancel, progress=progress), cancel
    )
    rows, bindings, issues = [], [], list(result.issues)
    for vault in result.vaults:
        if cancel.is_set():
            raise SyncCancelled("已取消发现目标仓库。")
        try:
            binding = DirectoryBinding.capture(vault.path)
            observed = getattr(vault, "observed_identity", None)
            if observed is not None and observed != binding.identity:
                raise SyncError("仓库目录在读取创建时间后已改变。")
            marker = assert_plain_chain(os.path.join(binding.physical, ".obsidian"))
            if marker is None or marker["kind"] != "dir" or not root_binding.contains(binding):
                raise SyncError("仓库标记已改变或路径已经超出集合范围。")
        except (OSError, SyncError) as exc:
            issues.append(ManagementIssue("target_identity_unavailable", str(exc), vault.path))
            continue
        bindings.append(binding)
        rows.append(vault)

    if not root_binding.matches():
        issues.append(ManagementIssue("collection_changed", "集合目录在发现期间已改变，请重新刷新。", root))
        return TargetCatalog(root, None, VaultCatalogResult(issues=tuple(issues)))
    return TargetCatalog(root, root_binding, VaultCatalogResult(tuple(rows), tuple(issues)), tuple(bindings))
