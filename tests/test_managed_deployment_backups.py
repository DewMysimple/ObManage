import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from obmanage.management import deployment
from obmanage.management.deployment import (
    DeploymentComponent, DeploymentEngine, DeploymentRequest, DeploymentSelection,
    DeploymentTarget,
)
from obmanage.management.journal import DeploymentJournal, JournalBatch, JournalError
from obmanage.management.recovery import require_recovery_clear
from obmanage.management.transactions import (
    complete_transactions, inspect_transaction_completion, maintain_transaction_backups,
    managed_backup_dir,
)
from obmanage.models import SyncError
from obmanage.paths import identity, snapshot


def prepare(tmp_path):
    source, target, state = (tmp_path / directory for directory in ("source", "vault", "state"))
    (source / ".obsidian").mkdir(parents=True)
    (source / ".obsidian/config.json").write_bytes(b"new config")
    (target / ".obsidian/empty").mkdir(parents=True)
    (target / ".obsidian/config.json").write_bytes(b"original config")
    (target / "note.md").write_bytes(b"untouched note")
    engine = DeploymentEngine(state)
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))),)))
    assert not state.exists()
    return engine, plan, source, target, state


def deploy(tmp_path):
    engine, plan, source, target, state = prepare(tmp_path)
    result = engine.execute(plan)
    assert result.success, result.errors
    return engine, plan.batch_id, source, target, state


def backup_name(batch_id, selection_id, suffix):
    return f".obmanage-deploy-{batch_id.replace('-', '')}-{selection_id.replace('-', '')}.{suffix}"


def managed_copy(state, batch_id, record, suffix):
    return managed_backup_dir(state) / backup_name(batch_id, record.selection_id, suffix)


def seed_legacy_deployment(tmp_path):
    engine, plan, source, target, state = prepare(tmp_path)
    record = deployment._journal_target(plan.targets[0])
    live = target / ".obsidian"
    backup = target / backup_name(plan.batch_id, record.selection_id, "backup")
    live.rename(backup)
    shutil.copytree(source / ".obsidian", live)
    target_record = replace(
        record, phase="committed", backup_path=str(backup),
        backup_identity=identity(snapshot(backup)), deployed_identity=identity(snapshot(live)),
    )
    engine.journal.create(JournalBatch(plan.batch_id, plan.label, "committed", (target_record,)))
    return engine, plan.batch_id, target, state, backup


def maintenance(state, days=None):
    return maintain_transaction_backups(state, days, Event())


def simulate_distinct_backup_volume(monkeypatch, state):
    volume_identity = deployment.volume_identity
    archive_root = managed_backup_dir(state)

    def volumes(path):
        if Path(path).is_relative_to(archive_root):
            return "isolated program volume"
        return volume_identity(path)

    monkeypatch.setattr(deployment, "volume_identity", volumes)


def test_cross_volume_migration_preserves_legacy_tree_with_hardlinked_file(tmp_path, monkeypatch):
    engine, batch_id, target, state, backup = seed_legacy_deployment(tmp_path)
    external_link = tmp_path / "other-link.json"
    os.link(backup / "config.json", external_link)
    simulate_distinct_backup_volume(monkeypatch, state)

    result = maintenance(state)

    assert result.moved == 0 and any("多重硬链接" in error for error in result.errors)
    assert engine.get_batch(batch_id).targets[0].managed_backup is None
    assert (backup / "config.json").samefile(external_link)
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"


def test_rollback_rechecks_session_after_preparing_cross_volume_restore(tmp_path, monkeypatch):
    engine, batch_id, _, target, state = deploy(tmp_path)
    simulate_distinct_backup_volume(monkeypatch, state)
    preview = engine.inspect_recovery(batch_id)
    assert preview.can_rollback

    def verify_session(paths):
        assert paths == (str(target),)
        if engine.get_batch(batch_id).targets[0].restore_identity is not None:
            raise deployment.DeploymentError("target reopened during restore staging")

    result = engine.rollback(batch_id, preview=preview, safety_check=verify_session)

    assert not result.success and "target reopened" in result.errors[0]
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"
    assert not tuple(target.glob("*.rollback"))
    retry = engine.rollback(batch_id, preview=engine.inspect_recovery(batch_id))
    assert retry.success, retry.errors
    assert (target / ".obsidian/config.json").read_bytes() == b"original config"


def test_deployment_and_preserved_rollback_keep_copies_outside_the_vault(tmp_path):
    engine, batch_id, source, target, state = deploy(tmp_path)
    live = target / ".obsidian"
    record = engine.get_batch(batch_id).targets[0]
    backup = managed_copy(state, batch_id, record, "backup")
    assert backup.parent == state / "backups"
    assert record.managed_backup is not None and record.managed_backup.ready
    assert identity(snapshot(backup)) == record.managed_backup.identity
    assert (backup / "config.json").read_bytes() == b"original config"
    assert (backup / "empty").is_dir()
    assert not tuple(target.glob(".obmanage-deploy-*"))

    (live / "workspace.json").write_bytes(b"live edits made after synchronization")
    reopened = DeploymentEngine(state)
    preview = reopened.inspect_recovery(batch_id)
    assert preview.can_rollback
    assert str(backup) in "\n".join(preview.details)
    rollback = reopened.rollback(batch_id, preview=preview)
    assert rollback.success, rollback.errors

    record = reopened.get_batch(batch_id).targets[0]
    preserved = managed_copy(state, batch_id, record, "rollback")
    assert record.managed_rollback is not None and record.managed_rollback.ready
    assert identity(snapshot(preserved)) == record.managed_rollback.identity
    assert (preserved / "workspace.json").read_bytes() == b"live edits made after synchronization"
    assert (preserved / "config.json").read_bytes() == b"new config"
    assert (live / "config.json").read_bytes() == b"original config"
    assert (live / "empty").is_dir()
    assert not tuple(target.glob(".obmanage-deploy-*"))
    assert (target / "note.md").read_bytes() == b"untouched note"
    assert (source / ".obsidian/config.json").read_bytes() == b"new config"
    require_recovery_clear(state)

    completion = inspect_transaction_completion(state, Event())
    result = complete_transactions(state, completion, Event())
    assert result.success and result.removed == 1
    assert reopened.list_batches() == ()
    assert not backup.exists() and not preserved.exists()
    assert (live / "config.json").read_bytes() == b"original config"


def test_authenticated_legacy_backup_migrates_once_without_age_setting(tmp_path):
    engine, batch_id, target, state, local_backup = seed_legacy_deployment(tmp_path)
    unrelated = target / ".obmanage-deploy-unregistered.backup"
    unrelated.mkdir()
    (unrelated / "canary").write_bytes(b"keep")
    assert engine.get_batch(batch_id).targets[0].managed_backup is None

    first = maintenance(state)
    assert first.moved == 1 and first.removed == 0 and not first.errors
    record = engine.get_batch(batch_id).targets[0]
    backup = managed_copy(state, batch_id, record, "backup")
    assert (backup / "config.json").read_bytes() == b"original config"
    assert not local_backup.exists()
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"
    assert (unrelated / "canary").read_bytes() == b"keep"

    revision = engine.get_batch(batch_id).revision
    second = maintenance(state)
    assert second.moved == 0 and second.removed == 0 and not second.errors
    assert engine.get_batch(batch_id).revision == revision
    assert engine.inspect_recovery(batch_id).can_rollback


def test_existing_unregistered_destination_is_never_adopted(tmp_path):
    engine, batch_id, target, state, local_backup = seed_legacy_deployment(tmp_path)
    record = engine.get_batch(batch_id).targets[0]
    occupied = managed_copy(state, batch_id, record, "backup")
    occupied.parent.mkdir()
    shutil.copytree(local_backup, occupied)
    occupied_identity = identity(snapshot(occupied))

    result = maintenance(state)

    assert result.moved == 0 and result.errors
    assert engine.get_batch(batch_id).targets[0].managed_backup is None
    assert identity(snapshot(occupied)) == occupied_identity
    assert (occupied / "config.json").read_bytes() == b"original config"
    assert (local_backup / "config.json").read_bytes() == b"original config"
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"


def test_restart_finishes_archiving_an_already_transferred_backup(tmp_path, monkeypatch):
    engine, batch_id, target, state, local = seed_legacy_deployment(tmp_path)
    record = engine.get_batch(batch_id).targets[0]
    destination = managed_copy(state, batch_id, record, "backup")
    set_target = DeploymentJournal.set_target

    def fail_to_publish(journal, identifier, selection_id, **changes):
        copy = changes.get("managed_backup")
        if copy is not None and copy.ready:
            assert (destination / "config.json").read_bytes() == b"original config"
            raise JournalError("injected failure after transfer")
        return set_target(journal, identifier, selection_id, **changes)

    with monkeypatch.context() as patch:
        patch.setattr(DeploymentJournal, "set_target", fail_to_publish)
        result = maintenance(state)
    assert result.moved == 0
    assert any("injected failure after transfer" in error for error in result.errors)
    record = engine.get_batch(batch_id).targets[0]
    assert record.managed_backup is not None and not record.managed_backup.ready

    retry = maintenance(state)
    assert retry.moved == 1 and not retry.errors
    assert engine.get_batch(batch_id).targets[0].managed_backup.ready
    assert not local.exists()
    assert (destination / "config.json").read_bytes() == b"original config"
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"
    assert DeploymentEngine(state).inspect_recovery(batch_id).can_rollback


def test_cross_volume_transfer_retries_an_incomplete_verified_stage(tmp_path, monkeypatch):
    engine, batch_id, target, state, local = seed_legacy_deployment(tmp_path)
    simulate_distinct_backup_volume(monkeypatch, state)
    record = engine.get_batch(batch_id).targets[0]
    destination = managed_copy(state, batch_id, record, "backup")
    frozen_copy = DeploymentEngine._copy_frozen_tree

    def interrupt_copy(engine, tree, stage, *args, **kwargs):
        if Path(stage) == destination:
            (destination / "config.json").write_bytes(b"ori")
            raise OSError("injected interruption during copy")
        return frozen_copy(engine, tree, stage, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(DeploymentEngine, "_copy_frozen_tree", interrupt_copy)
        result = maintenance(state)
    assert result.moved == 0
    assert any("injected interruption during copy" in error for error in result.errors)
    assert (local / "config.json").read_bytes() == b"original config"
    assert (destination / "config.json").read_bytes() == b"ori"
    pending = engine.get_batch(batch_id).targets[0].managed_backup
    assert pending is not None and not pending.ready
    assert identity(snapshot(destination)) == pending.identity

    retry = maintenance(state)
    assert retry.moved == 1 and not retry.errors
    assert identity(snapshot(destination)) == pending.identity
    assert (destination / "config.json").read_bytes() == b"original config"
    assert not local.exists()
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"


@pytest.mark.skipif(os.name != "nt", reason="uses Windows alternate data streams")
def test_named_stream_prevents_portable_transfer_and_automatic_expiration(tmp_path, monkeypatch):
    engine, batch_id, target, state, local = seed_legacy_deployment(tmp_path)
    if not deployment.volume_identity(str(local)).casefold().endswith(":ntfs"):
        pytest.skip("requires an NTFS temporary directory")
    stream_name = ":obmanage-test-stream"
    stream_data = b"additional data outside the portable manifest"
    local_stream = Path(str(local / "config.json") + stream_name)
    local_stream.write_bytes(stream_data)
    row = engine.get_batch(batch_id).targets[0]
    backup = managed_copy(state, batch_id, row, "backup")

    with monkeypatch.context() as patch:
        simulate_distinct_backup_volume(patch, state)
        result = maintenance(state)
    assert result.moved == 0 and any("备用数据流" in error for error in result.errors)
    assert not backup.exists() and local_stream.read_bytes() == stream_data
    assert (local / "config.json").read_bytes() == b"original config"
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"

    result = maintenance(state)
    archived_stream = Path(str(backup / "config.json") + stream_name)
    assert result.moved == 1 and not result.errors
    assert not local.exists() and archived_stream.read_bytes() == stream_data
    row = engine.get_batch(batch_id).targets[0]
    monkeypatch.setattr(time, "time", lambda: row.managed_backup.retained_since + 2 * 86_400)

    result = maintenance(state, 1)

    assert result.removed == 0 and any("备用数据流" in error for error in result.errors)
    assert archived_stream.read_bytes() == stream_data
    assert engine.get_batch(batch_id).status == "committed"
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"


@pytest.mark.parametrize("when", ["before-transfer", "during-transfer"])
def test_cancel_during_immediate_archive_keeps_committed_deployment(tmp_path, monkeypatch, when):
    engine, plan, _, target, state = prepare(tmp_path)
    cancel_event = Event()
    archive_root = managed_backup_dir(state)
    copy_frozen = DeploymentEngine._copy_frozen_tree

    def cancel_at_commit(progress):
        if when == "before-transfer" and progress.phase == "committed":
            cancel_event.set()

    def interrupt_archive(engine, tree, stage, cancel, *args, **kwargs):
        destination = Path(stage)
        if destination.is_relative_to(archive_root):
            (destination / "config.json").write_bytes(b"ori")
            cancel_event.set()
        return copy_frozen(engine, tree, stage, cancel, *args, **kwargs)

    if when == "during-transfer":
        simulate_distinct_backup_volume(monkeypatch, state)
    with monkeypatch.context() as patch:
        if when == "during-transfer":
            patch.setattr(DeploymentEngine, "_copy_frozen_tree", interrupt_archive)
        result = engine.execute(plan, cancel=cancel_event, progress=cancel_at_commit)

    assert cancel_event.is_set()
    assert result.success and result.journal_status == "committed"
    assert result.committed_targets == 1 and result.rolled_back_targets == 0
    assert any("备份迁移已暂停" in error for error in result.errors)
    record = engine.get_batch(plan.batch_id).targets[0]
    local = Path(record.backup_path)
    assert (local / "config.json").read_bytes() == b"original config"
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"
    if when == "before-transfer":
        assert record.managed_backup is None
    else:
        assert record.managed_backup is not None and not record.managed_backup.ready

    retry = maintenance(state)
    assert retry.moved == 1 and not retry.errors
    destination = managed_copy(state, plan.batch_id, record, "backup")
    assert (destination / "config.json").read_bytes() == b"original config"
    assert not local.exists()
    assert engine.get_batch(plan.batch_id).status == "committed"


def test_cross_volume_rollback_retries_after_installing_authenticated_restore_stage(tmp_path, monkeypatch):
    engine, plan, _, target, state = prepare(tmp_path)
    simulate_distinct_backup_volume(monkeypatch, state)
    committed = engine.execute(plan)
    assert committed.success, committed.errors
    live = target / ".obsidian"
    (live / "workspace.json").write_bytes(b"keep later data")
    preview = engine.inspect_recovery(plan.batch_id)
    rename = deployment._rename_directory

    def interrupt_after_install(source, destination):
        rename(source, destination)
        if Path(destination) == live:
            raise RuntimeError("simulated termination after installation")

    with monkeypatch.context() as patch:
        patch.setattr(deployment, "_rename_directory", interrupt_after_install)
        with pytest.raises(RuntimeError, match="termination after installation"):
            engine.rollback(plan.batch_id, preview=preview)
    interrupted = engine.get_batch(plan.batch_id).targets[0]
    assert interrupted.restore_identity == identity(snapshot(live))
    assert (live / "config.json").read_bytes() == b"original config"

    reopened = DeploymentEngine(state)
    retry = reopened.rollback(plan.batch_id)
    assert retry.success, retry.errors
    finished = reopened.get_batch(plan.batch_id).targets[0]
    preserved = managed_copy(state, plan.batch_id, finished, "rollback")
    assert (preserved / "workspace.json").read_bytes() == b"keep later data"
    assert not tuple(target.glob(".obmanage-deploy-*"))
    require_recovery_clear(state)


def test_opt_in_expiration_uses_backup_age_and_leaves_live_edits_alone(tmp_path, monkeypatch):
    engine, batch_id, _, target, state = deploy(tmp_path)
    record = engine.get_batch(batch_id).targets[0]
    backup = managed_copy(state, batch_id, record, "backup")
    clock = record.managed_backup.retained_since
    monkeypatch.setattr(time, "time", lambda: clock)
    workspace = target / ".obsidian/workspace.json"
    workspace.write_bytes(b"retain live edits")
    unrelated = managed_backup_dir(state) / ".obmanage-deploy-unregistered.backup"
    unrelated.mkdir()
    (unrelated / "canary").write_bytes(b"never age out an unknown tree")

    clock += 24 * 60 * 60 - 1
    assert maintenance(state, 1).removed == 0
    clock += 5
    assert maintenance(state).removed == 0
    assert backup.is_dir()
    assert engine.get_batch(batch_id).status == "committed"

    expired = maintenance(state, 1)
    assert expired.removed == 1 and not expired.errors
    assert engine.list_batches() == () and not backup.exists()
    assert (target / ".obsidian/config.json").read_bytes() == b"new config"
    assert workspace.read_bytes() == b"retain live edits"
    assert (unrelated / "canary").read_bytes() == b"never age out an unknown tree"
    require_recovery_clear(state)


def test_expiration_retires_ended_rollback_preservation_after_its_retention_period(tmp_path, monkeypatch):
    engine, batch_id, _, target, state = deploy(tmp_path)
    workspace = target / ".obsidian/workspace.json"
    workspace.write_bytes(b"preserve later edits")
    recovered = engine.rollback(batch_id, preview=engine.inspect_recovery(batch_id))
    assert recovered.success, recovered.errors
    record = engine.get_batch(batch_id).targets[0]
    preserved = managed_copy(state, batch_id, record, "rollback")
    assert (preserved / "workspace.json").read_bytes() == b"preserve later edits"
    assert not workspace.exists()

    monkeypatch.setattr(time, "time", lambda: record.managed_rollback.retained_since + 2 * 86_400)
    result = maintenance(state, 1)

    assert result.removed == 1 and not result.errors
    assert not preserved.exists() and engine.list_batches() == ()
    assert (target / ".obsidian/config.json").read_bytes() == b"original config"
    assert (target / "note.md").read_bytes() == b"untouched note"


@pytest.mark.parametrize("condition", ["interrupted", "changed-copy", "changed-live-identity"])
def test_expiration_keeps_protected_backups_and_pending_recovery(tmp_path, monkeypatch, condition):
    engine, batch_id, _, target, state = deploy(tmp_path)
    record = engine.get_batch(batch_id).targets[0]
    backup = managed_copy(state, batch_id, record, "backup")
    monkeypatch.setattr(time, "time", lambda: record.managed_backup.retained_since + 2 * 86_400)
    live = target / ".obsidian"
    if condition == "interrupted":
        engine.journal.set_batch(batch_id, status="rollback_required")
    elif condition == "changed-copy":
        (backup / "unknown.md").write_bytes(b"protect unknown data")
    else:
        displaced = target / "displaced"
        live.rename(displaced)
        shutil.copytree(displaced, live)

    result = maintenance(state, 1)

    assert result.removed == 0
    assert (backup / "config.json").read_bytes() == b"original config"
    assert (live / "config.json").read_bytes() == b"new config"
    if condition == "changed-copy":
        assert (backup / "unknown.md").read_bytes() == b"protect unknown data"
    assert engine.get_batch(batch_id).status == (
        "rollback_required" if condition == "interrupted" else "committed"
    )
    with pytest.raises(SyncError):
        require_recovery_clear(state)


def test_expiration_rechecks_live_identity_after_hashing_managed_backup(tmp_path, monkeypatch):
    engine, batch_id, _, target, state = deploy(tmp_path)
    record = engine.get_batch(batch_id).targets[0]
    backup = managed_copy(state, batch_id, record, "backup")
    live = target / ".obsidian"
    displaced = target / "displaced"
    checked_manifest = deployment._assert_manifest
    monkeypatch.setattr(time, "time", lambda: record.managed_backup.retained_since + 2 * 86_400)

    def replace_live_after_backup_hash(root, *args, **kwargs):
        verified = checked_manifest(root, *args, **kwargs)
        if Path(root) == backup and not displaced.exists():
            live.rename(displaced)
            shutil.copytree(displaced, live)
        return verified

    def reject_deletion(*args, **kwargs):
        pytest.fail("take no backup deletion action once the live identity has changed")

    monkeypatch.setattr(deployment, "_assert_manifest", replace_live_after_backup_hash)
    monkeypatch.setattr(deployment, "_remove_owned_tree", reject_deletion)
    result = maintenance(state, 1)

    assert displaced.is_dir() and identity(snapshot(live)) != record.deployed_identity
    assert result.removed == 0 and result.errors
    assert engine.get_batch(batch_id).status == "committed"
    assert (backup / "config.json").read_bytes() == b"original config"
    assert (live / "config.json").read_bytes() == b"new config"
