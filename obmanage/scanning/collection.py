"""Collection discovery and per-vault projections from one observation epoch."""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..management.catalog import discover_vaults
from ..management.models import VaultCatalogResult
from ..paths import canonical
from .metrics import count
from .session import ManifestView, ScanSession


@dataclass(frozen=True)
class CollectionSnapshot:
    root: str
    catalog: VaultCatalogResult
    manifests: dict[str, ManifestView]
    epoch: str

    def catalog_signature(self):
        return (tuple((v.path, v.parent_path) for v in self.catalog.vaults),
                tuple((i.code, i.path, i.severity) for i in self.catalog.issues))

    def view(self, path):
        return self.manifests[os.path.normcase(canonical(path))]


def collect_collection(root, session: ScanSession, *, progress=None):
    catalog = discover_vaults(root, cancel=session.cancel, progress=progress, _session=session)
    manifests = {}
    for vault in catalog.vaults:
        view = session.manifest(vault.path)
        manifests[os.path.normcase(vault.path)] = view
    count("vault_observations", len(catalog.vaults))
    return CollectionSnapshot(canonical(root), catalog, manifests, session.epoch)
