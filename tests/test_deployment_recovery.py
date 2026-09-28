from __future__ import annotations

import json
from pathlib import Path

import pytest

import obmanage.management.deployment as deployment
from obmanage.management.deployment import (
    DeploymentComponent, DeploymentEngine, DeploymentRequest,
    DeploymentSelection, DeploymentTarget,
)
from obmanage.management.recovery import require_recovery_clear
from obmanage.models import SyncError


def contents(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def committed(tmp_path):
    source, target, state = (tmp_path / name for name in ("source", "target", "state"))
    source.mkdir()
    (target / ".obsidian").mkdir(parents=True)
    (source / "config.json").write_text("new")
    (target / ".obsidian" / "old.json").write_text("old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.direct("obsidian", source, ".obsidian"),
        DeploymentTarget("target", str(target))),), label="comsync"))
    assert engine.execute(plan).success
    return engine, plan.batch_id, source, target, state


def test_changed_config_can_end_transaction_after_both_old_actions_fail(committed):
    engine, batch_id, source, target, state = committed
    (target / ".obsidian" / "workspace.json").write_text("user session")
    before = contents(target)
    source_before = contents(source)
    assert not engine.rollback(batch_id).success
    assert not engine.finalize(batch_id).success
    preview = engine.inspect_recovery(batch_id)
    assert preview.can_rollback
    assert "已有变化" in "\n".join(preview.details)
    with pytest.raises(SyncError):
        require_recovery_clear(state)
    assert engine.resolve(batch_id, expected_revision=preview.revision).success
    assert contents(target) == before
    assert contents(source) == source_before
    require_recovery_clear(state)
    fresh = DeploymentEngine(state)
    assert fresh.get_batch(batch_id).status == "resolved"
    assert fresh.finalize(batch_id).success
    assert contents(target / ".obsidian") == {"config.json": b"new", "workspace.json": b"user session"}
    require_recovery_clear(state)


def test_preserved_rollback_restores_old_and_keeps_all_new_content(committed):
    engine, batch_id, source, target, state = committed
    (target / ".obsidian" / "config.json").write_text("edited after sync")
    (target / ".obsidian" / "added.txt").write_text("new note")
    expected = contents(target / ".obsidian")
    before = contents(target)
    revision = engine.get_batch(batch_id).revision
    preview = engine.inspect_recovery(batch_id)
    assert contents(target) == before  # inspection is strictly read-only
    assert engine.get_batch(batch_id).revision == revision
    result = DeploymentEngine(state).rollback(batch_id, preview=preview)
    assert result.success, result.errors
    assert result.journal_status == "rolled_back_with_residuals"
    assert contents(target / ".obsidian") == {"old.json": b"old"}
    saved = engine.get_batch(batch_id).targets[0]
    assert contents(Path(saved.rollback_path)) == expected
    assert saved.preserved_manifest is not None
    assert contents(source) == {"config.json": b"new"}
    require_recovery_clear(state)
    assert not engine.finalize(batch_id).success
    assert contents(Path(saved.rollback_path)) == expected
    require_recovery_clear(state)  # an invalid cleanup must not reopen recovery


def test_stale_preserved_rollback_rejected_before_target_write(committed):
    engine, batch_id, _, target, _ = committed
    preview = engine.inspect_recovery(batch_id)
    (target / ".obsidian" / "later.txt").write_text("later")
    before = contents(target)
    result = engine.rollback(batch_id, preview=preview)
    assert not result.success
    assert "变化" in result.errors[0]
    assert contents(target) == before


def test_old_inspection_cannot_acknowledge_new_transaction_state(committed):
    engine, batch_id, _, target, state = committed
    preview = engine.inspect_recovery(batch_id)
    engine.journal.set_batch(batch_id, error="another process updated recovery")
    assert not engine.resolve(batch_id, expected_revision=preview.revision).success
    with pytest.raises(SyncError):
        require_recovery_clear(state)


def test_bad_backup_blocks_rollback_but_can_resolve_without_deleting_anything(committed):
    engine, batch_id, _, target, state = committed
    backup = Path(engine.get_batch(batch_id).targets[0].backup_path)
    (backup / "unknown.txt").write_text("do not delete")
    before = contents(target)
    preview = engine.inspect_recovery(batch_id)
    assert not preview.can_rollback
    assert engine.resolve(batch_id, expected_revision=preview.revision).success
    result = engine.finalize(batch_id)
    assert not result.success
    assert result.journal_status == "resolved"
    assert contents(target) == before
    require_recovery_clear(state)


def test_disconnected_target_can_resolve_without_target_io(committed, monkeypatch):
    engine, batch_id, _, target, state = committed
    before = contents(target)
    def no_disk(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(deployment, "volume_identity", no_disk)
    preview = engine.inspect_recovery(batch_id)
    assert not preview.can_rollback
    assert engine.resolve(batch_id, expected_revision=preview.revision).success
    assert contents(target) == before
    require_recovery_clear(state)


def test_tampered_journal_cannot_be_resolved(committed):
    engine, batch_id, _, target, state = committed
    preview = engine.inspect_recovery(batch_id)
    path = state / "deployment-journal" / f"{batch_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["errors"].append("tampered")
    path.write_text(json.dumps(data), encoding="utf-8")
    before = contents(target)
    assert not engine.resolve(batch_id, expected_revision=preview.revision).success
    assert contents(target) == before
    with pytest.raises(SyncError):
        require_recovery_clear(state)


@pytest.mark.parametrize("crash_point", ["move", "restore"])
def test_restart_continues_preserved_rollback_without_losing_copies(committed, monkeypatch, crash_point):
    engine, batch_id, _, target, state = committed
    (target / ".obsidian" / "late.txt").write_text("late")
    expected = contents(target / ".obsidian")
    preview = engine.inspect_recovery(batch_id)
    rename = deployment._rename_directory
    class Crash(BaseException):
        pass
    def crash(source, destination):
        rename(source, destination)
        if ((crash_point == "move" and str(destination).endswith(".rollback")) or
                (crash_point == "restore" and str(source).endswith(".backup"))):
            raise Crash()
    monkeypatch.setattr(deployment, "_rename_directory", crash)
    with pytest.raises(Crash):
        engine.rollback(batch_id, preview=preview)
    monkeypatch.setattr(deployment, "_rename_directory", rename)
    fresh = DeploymentEngine(state)
    result = fresh.rollback(batch_id)
    assert result.success, result.errors
    saved = fresh.get_batch(batch_id).targets[0]
    assert contents(Path(saved.rollback_path)) == expected
    assert contents(target / ".obsidian") == {"old.json": b"old"}
    require_recovery_clear(state)


def test_resolved_cleanup_never_deletes_preserved_rollback_content(committed, monkeypatch):
    engine, batch_id, _, target, state = committed
    expected = contents(target / ".obsidian")
    preview = engine.inspect_recovery(batch_id)
    rename = deployment._rename_directory
    def fail_restore(source, destination):
        if str(source).endswith(".backup"):
            raise RuntimeError("simulated process crash")
        rename(source, destination)
    monkeypatch.setattr(deployment, "_rename_directory", fail_restore)
    with pytest.raises(RuntimeError):
        engine.rollback(batch_id, preview=preview)
    preview = engine.inspect_recovery(batch_id)
    assert engine.resolve(batch_id, expected_revision=preview.revision).success
    assert engine.finalize(batch_id).success
    saved = engine.get_batch(batch_id).targets[0]
    assert contents(Path(saved.rollback_path)) == expected
    require_recovery_clear(state)


def test_resolved_batch_cannot_rollback_over_a_later_deployment(committed):
    engine, batch_id, source, target, state = committed
    assert engine.resolve(batch_id, expected_revision=engine.get_batch(batch_id).revision).success
    (source / "config.json").write_text("second deployment")
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.direct("obsidian", source, ".obsidian"),
        DeploymentTarget("target", str(target))),), label="comsync"))
    assert engine.execute(plan).success
    before = contents(target)
    assert not engine.rollback(batch_id).success
    assert contents(target) == before
    with pytest.raises(SyncError):
        require_recovery_clear(state)  # the second batch still needs a decision


def test_replaced_live_root_is_not_adopted_for_rollback(committed):
    engine, batch_id, _, target, _ = committed
    (target / ".obsidian").rename(target / "displaced")
    (target / ".obsidian").mkdir()
    (target / ".obsidian" / "user.txt").write_text("new root")
    before = contents(target)
    preview = engine.inspect_recovery(batch_id)
    assert not preview.can_rollback
    assert not engine.rollback(batch_id, preview=preview).success
    assert contents(target) == before


def test_optional_cleanup_retries_partial_deletion_without_reblocking(committed, monkeypatch):
    engine, batch_id, _, target, state = committed
    assert engine.resolve(batch_id, expected_revision=engine.get_batch(batch_id).revision).success
    preview = engine.inspect_recovery(batch_id)
    original = deployment._unlink_owned_file
    def fail_after_delete(path, expected):
        original(path, expected)
        raise OSError("injected failure after deleting one file")
    monkeypatch.setattr(deployment, "_unlink_owned_file", fail_after_delete)
    assert not engine.finalize(batch_id, preview=preview).success
    require_recovery_clear(state)
    monkeypatch.setattr(deployment, "_unlink_owned_file", original)
    fresh = DeploymentEngine(state)
    preview = fresh.inspect_recovery(batch_id)
    assert fresh.finalize(batch_id, preview=preview).success
    assert contents(target / ".obsidian") == {"config.json": b"new"}
    require_recovery_clear(state)


def test_cleanup_preview_rejects_changed_backup(committed):
    engine, batch_id, _, target, state = committed
    assert engine.resolve(batch_id, expected_revision=engine.get_batch(batch_id).revision).success
    preview = engine.inspect_recovery(batch_id)
    backup = Path(engine.get_batch(batch_id).targets[0].backup_path)
    (backup / "old.json").write_text("user changed backup")
    before = contents(target)
    assert not engine.finalize(batch_id, preview=preview).success
    assert contents(target) == before
    require_recovery_clear(state)


def test_legacy_record_without_preservation_field_remains_readable(committed):
    import hashlib
    import hmac
    import obmanage.management.journal as journal
    engine, batch_id, _, _, state = committed
    path = state / "deployment-journal" / f"{batch_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop(journal.AUTHENTICATION_FIELD)
    for target in document["targets"]:
        target.pop("preserved_manifest")
    key = (state / "deployment-journal.key").read_bytes()
    document[journal.AUTHENTICATION_FIELD] = {
        "algorithm": "hmac-sha256",
        "mac": hmac.new(key, journal._canonical_json(document), hashlib.sha256).hexdigest(),
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    preview = DeploymentEngine(state).inspect_recovery(batch_id)
    assert preview.can_rollback
    assert engine.resolve(batch_id, expected_revision=preview.revision).success
    require_recovery_clear(state)


def test_all_recovery_targets_are_rechecked_before_the_first_move(tmp_path):
    source, state = tmp_path / "source", tmp_path / "state"
    source.mkdir()
    (source / "new.txt").write_text("new")
    targets = (tmp_path / "a", tmp_path / "b")
    for target in targets:
        (target / ".obsidian").mkdir(parents=True)
        (target / ".obsidian" / "old.txt").write_text("old")
    engine = DeploymentEngine(state)
    request = DeploymentRequest(tuple(DeploymentSelection(
        DeploymentComponent.direct("obsidian", source, ".obsidian"),
        DeploymentTarget(str(index), str(target))) for index, target in enumerate(targets)))
    plan = engine.analyze(request)
    assert engine.execute(plan).success
    preview = engine.inspect_recovery(plan.batch_id)
    (targets[0] / ".obsidian" / "late.txt").write_text("late")
    before = [contents(target) for target in targets]
    assert not engine.rollback(plan.batch_id, preview=preview).success
    assert [contents(target) for target in targets] == before


@pytest.mark.parametrize("rollback", [False, True])
def test_clear_ended_transaction_removes_all_copies_and_record(committed, rollback):
    engine, batch_id, source, target, state = committed
    (target / ".obsidian" / "later.md").write_text("keep")
    preview = engine.inspect_recovery(batch_id)
    assert not engine.clear_transaction(batch_id, preview=preview).success
    if rollback:
        assert engine.rollback(batch_id, preview=preview).success
    else:
        assert engine.resolve(batch_id, expected_revision=preview.revision).success
    current = contents(target / ".obsidian")
    original_source = contents(source)
    batch = engine.get_batch(batch_id)
    paths = [Path(path) for t in batch.targets for path in
             (t.backup_path, t.stage_path, t.rollback_path) if path]
    preview = engine.inspect_recovery(batch_id)
    assert not preview.cleanup_error
    assert engine.clear_transaction(batch_id, preview=preview).success
    assert all(not path.exists() for path in paths)
    assert not Path(engine.journal._path(batch_id)).exists()
    assert not Path(engine.journal._grant_path(batch_id)).exists()
    assert DeploymentEngine(state).list_batches() == ()
    assert contents(target / ".obsidian") == current
    assert contents(source) == original_source
    require_recovery_clear(state)


def test_clear_rejects_changed_copy_and_retains_record(committed):
    engine, batch_id, _, _, _ = committed
    assert engine.resolve(batch_id, expected_revision=engine.get_batch(batch_id).revision).success
    preview = engine.inspect_recovery(batch_id)
    backup = Path(engine.get_batch(batch_id).targets[0].backup_path)
    (backup / "unknown.txt").write_text("user content")
    assert not engine.clear_transaction(batch_id, preview=preview).success
    assert (backup / "old.json").exists()
    assert (backup / "unknown.txt").read_text() == "user content"
    assert engine.get_batch(batch_id).status == "resolved"


@pytest.mark.parametrize("suffix", [".grant.json", ".json"])
def test_clear_metadata_unlink_failure_is_terminal_and_restart_retryable(committed, monkeypatch, suffix):
    import obmanage.management.journal as journal
    engine, batch_id, _, _, state = committed
    assert engine.resolve(batch_id, expected_revision=engine.get_batch(batch_id).revision).success
    original = journal.os.unlink
    failed_path = engine.journal._grant_path(batch_id) if suffix == ".grant.json" else engine.journal._path(batch_id)
    def fail(path, *args, **kwargs):
        if journal.canonical(path) == journal.canonical(failed_path):
            raise PermissionError("injected metadata lock")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(journal.os, "unlink", fail)
        assert not engine.clear_transaction(batch_id, preview=engine.inspect_recovery(batch_id)).success
    fresh = DeploymentEngine(state)
    assert fresh.list_batches()[0].status == "discarded"
    require_recovery_clear(state)
    assert fresh.clear_transaction(batch_id, preview=fresh.inspect_recovery(batch_id)).success
    assert fresh.list_batches() == ()


def test_clear_copy_failure_retains_retryable_record(committed, monkeypatch):
    engine, batch_id, _, _, state = committed
    assert engine.resolve(batch_id, expected_revision=engine.get_batch(batch_id).revision).success
    with monkeypatch.context() as patch:
        patch.setattr(deployment, "_remove_owned_tree", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("locked")))
        assert not engine.clear_transaction(batch_id, preview=engine.inspect_recovery(batch_id)).success
    assert engine.get_batch(batch_id).status == "resolved"
    require_recovery_clear(state)
    assert engine.clear_transaction(batch_id, preview=engine.inspect_recovery(batch_id)).success


def test_clear_stale_revision_and_tampered_journal_are_rejected(committed):
    engine, batch_id, _, _, _ = committed
    assert engine.resolve(batch_id, expected_revision=engine.get_batch(batch_id).revision).success
    preview = engine.inspect_recovery(batch_id)
    engine.journal.set_batch(batch_id, error="changed")
    assert not engine.clear_transaction(batch_id, preview=preview).success
    preview = engine.inspect_recovery(batch_id)
    path = Path(engine.journal._path(batch_id))
    value = json.loads(path.read_text(encoding="utf-8"))
    value["errors"] = ["tampered"]
    path.write_text(json.dumps(value), encoding="utf-8")
    assert not engine.clear_transaction(batch_id, preview=preview).success
    assert Path(engine.journal._grant_path(batch_id)).exists()


def test_forget_ended_offline_record_leaves_all_data_unchanged(committed, monkeypatch):
    engine, batch_id, source, target, state = committed
    revision = engine.get_batch(batch_id).revision
    assert not engine.forget_transaction(batch_id, expected_revision=revision).success
    assert engine.resolve(batch_id, expected_revision=revision).success
    revision = engine.get_batch(batch_id).revision
    before = contents(target)
    with monkeypatch.context() as patch:
        patch.setattr(deployment, "volume_identity", lambda *_: (_ for _ in ()).throw(OSError("offline")))
        assert engine.forget_transaction(batch_id, expected_revision=revision).success
    assert contents(target) == before
    assert DeploymentEngine(state).list_batches() == ()
    require_recovery_clear(state)
