import hashlib
import hmac
import json
import uuid
from pathlib import Path

import pytest

from obmanage.management.journal import (
    AUTHENTICATION_FIELD,
    DeploymentJournal,
    JournalBatch,
    JournalError,
    JournalTarget,
    ManagedCopy,
    _canonical_json,
)


def test_managed_copies_upgrade_a_signed_legacy_record_without_replacing_its_grant(tmp_path):
    journal = DeploymentJournal(tmp_path / "state")
    target_root = tmp_path / "target"
    source_root = tmp_path / "source"
    manifest = {"": {"kind": "dir"}}
    target = JournalTarget(
        selection_id=str(uuid.uuid4()), component_id="obsidian",
        source_path=str(source_root / ".obsidian"), source_authorization_root=str(source_root),
        source_volume="source-volume", target_root=str(target_root),
        target_relative=".obsidian", target_path=str(target_root / ".obsidian"),
        target_volume="target-volume", target_root_identity=("dir", 0, 1),
        original_identity=("dir", 0, 2), phase="committed",
        source_manifest=manifest, original_manifest=manifest, deployed_manifest=manifest,
    )
    batch_id = str(uuid.uuid4())
    journal.create(JournalBatch(batch_id, ".comSync", "committed", (target,)))
    record_path = Path(journal.directory) / f"{batch_id}.json"
    grant_path = Path(journal.grant_directory) / f"{batch_id}.grant.json"
    grant_bytes = grant_path.read_bytes()

    # Reproduce the original schema and its full-record MAC.
    legacy = json.loads(record_path.read_bytes())
    authentication = legacy.pop(AUTHENTICATION_FIELD)
    for key in ("managed_backup", "managed_rollback", "restore_identity"):
        legacy["targets"][0].pop(key)
    key = Path(journal.key_path).read_bytes()
    authentication["mac"] = hmac.new(key, _canonical_json(legacy), hashlib.sha256).hexdigest()
    legacy[AUTHENTICATION_FIELD] = authentication
    record_path.write_bytes(_canonical_json(legacy))

    assert journal.get(batch_id).targets == (target,)
    backup = ManagedCopy(("dir", 3, 4), ready=True, retained_since=100.0)
    rollback = ManagedCopy(("dir", 3, 5), ready=True, retained_since=200.0)
    journal.set_target(batch_id, target.selection_id, managed_backup=backup,
                       managed_rollback=rollback, preserved_manifest=manifest,
                       restore_identity=("dir", 0, 6))
    read = journal.get(batch_id).targets[0]
    assert read.managed_backup == backup
    assert read.managed_rollback == rollback
    assert read.restore_identity == ("dir", 0, 6)
    assert grant_path.read_bytes() == grant_bytes

    tampered = json.loads(record_path.read_bytes())
    tampered["targets"][0]["managed_backup"]["retained_since"] = 1.0
    record_path.write_bytes(_canonical_json(tampered))
    with pytest.raises(JournalError, match="完整性认证失败"):
        journal.get(batch_id)
