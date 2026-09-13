from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest

import obmanage.management.trash as trash_module
from obmanage.management.trash import TrashCleanupEngine
from obmanage.paths import native


@pytest.fixture
def seed_legacy_quarantine():
    """Return a helper that creates authentic 2.0.0 state for upgrade tests."""

    def seed(state_dir: Path, vault: Path) -> tuple[str, Path]:
        engine = TrashCleanupEngine(state_dir)
        preview = engine.analyze((vault,)).vaults[0]
        engine._ensure_state_dirs()
        engine._auth_key(create=True)
        operation_id = str(uuid.uuid4())
        operation_root = engine._operation_backup_root(operation_id)
        os.mkdir(native(operation_root))
        operation_snapshot = trash_module._read_snapshot(operation_root, "dir")
        backup_rel = "vault_0000"
        backup_path = engine._backup_path(operation_id, backup_rel)
        os.mkdir(native(backup_path))
        backup_snapshot = trash_module._read_snapshot(backup_path, "dir")
        trash_module._copy_entries(
            preview.trash_path,
            backup_path,
            preview.entries,
            None,
            None,
            preview.vault_root,
            "backup",
            destination_exists=True,
        )
        journal = {
            "version": trash_module.JOURNAL_VERSION,
            "operation_id": operation_id,
            "created_at": time.time(),
            "backup_root_snapshot": asdict(operation_snapshot),
            "vaults": [{
                "backup_rel": backup_rel,
                "backup_snapshot": asdict(backup_snapshot),
                "status": "backed_up",
                "freed_bytes": 0,
                "preview": trash_module._preview_to_json(preview),
            }],
        }
        engine._save_journal(journal)
        result, failures = trash_module._clear_preview(preview, None, None)
        assert result.status == "success"
        assert not failures
        journal["vaults"][0]["status"] = "success"
        engine._save_journal(journal)
        return operation_id, Path(backup_path)

    return seed
