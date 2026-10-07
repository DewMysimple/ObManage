from threading import Event

import pytest

from obmanage.management.deployment import (
    DeploymentComponent, DeploymentEngine, DeploymentRequest, DeploymentSelection, DeploymentTarget,
)
from obmanage.management.transactions import clear_transactions, inspect_transaction_clear
from obmanage.management.trash import TrashCleanupEngine, TrashSafetyError


def make_quarantine(root, seed, name="vault"):
    state, vault = root / "state", root / name
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash/note.md").write_text(name)
    identifier, backup = seed(state, vault)
    return identifier, backup, vault


def deploy(root, *, end=True):
    source, target, state = (root / name for name in ("source", "target", "state"))
    for vault in (source, target):
        (vault / ".obsidian").mkdir(parents=True, exist_ok=True)
    (source / ".obsidian/config.json").write_text("new")
    (target / ".obsidian/config.json").write_text("old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))),)))
    assert engine.execute(plan).success
    if end:
        assert engine.resolve(plan.batch_id, expected_revision=engine.get_batch(plan.batch_id).revision).success
    return engine, plan.batch_id, target


def test_bulk_clear_removes_all_three_completed_records_and_paths(tmp_path, seed_legacy_quarantine):
    trash = TrashCleanupEngine(tmp_path / "state")
    seeds = [make_quarantine(tmp_path, seed_legacy_quarantine, str(i)) for i in range(3)]
    for identifier, _, _ in seeds:
        assert trash.finalize(identifier).status == "success"
    before = {p: p.read_bytes() for p in (tmp_path / "state").rglob("*.json")}
    preview = inspect_transaction_clear(tmp_path / "state", Event())
    assert preview.count == 3
    assert {p: p.read_bytes() for p in (tmp_path / "state").rglob("*.json")} == before
    result = clear_transactions(tmp_path / "state", preview, Event())
    assert result.success and result.removed == 3
    assert TrashCleanupEngine(tmp_path / "state").list_operations() == ()
    assert not tuple((tmp_path / "state/trash_journal").iterdir())
    assert not tuple((tmp_path / "state/trash_backups").iterdir())
    assert all((vault / ".trash").is_dir() for _, _, vault in seeds)


def test_pending_deployment_is_excluded_but_all_other_records_clear(tmp_path, seed_legacy_quarantine):
    identifier, backup, vault = make_quarantine(tmp_path, seed_legacy_quarantine)
    engine, batch_id, target = deploy(tmp_path, end=False)
    revision = engine.get_batch(batch_id).revision
    preview = inspect_transaction_clear(tmp_path / "state", Event())
    assert preview.count == 1 and preview.pending_deployments == 1
    assert clear_transactions(tmp_path / "state", preview, Event()).success
    assert engine.get_batch(batch_id).revision == revision
    assert tuple(target.glob(".obmanage-deploy-*.backup"))
    assert not backup.exists()
    assert (target / ".obsidian/config.json").read_text() == "new"


def test_clear_keeps_new_unconfirmed_transactions(tmp_path, seed_legacy_quarantine):
    first, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine, "first")
    preview = inspect_transaction_clear(tmp_path / "state", Event())
    second, other_backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine, "second")
    assert clear_transactions(tmp_path / "state", preview, Event()).success
    assert not backup.exists()
    assert other_backup.exists()
    assert [op.operation_id for op in TrashCleanupEngine(tmp_path / "state").list_operations()] == [second]


def test_changed_quarantine_record_is_rejected_without_deleting_copies(tmp_path, seed_legacy_quarantine):
    identifier, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    preview = inspect_transaction_clear(tmp_path / "state", Event())
    trash = TrashCleanupEngine(tmp_path / "state")
    data = trash._load_journal(identifier)
    data["created_at"] += 1
    trash._save_journal(data)
    result = clear_transactions(tmp_path / "state", preview, Event())
    assert not result.success and result.removed == 0
    assert "发生变化" in result.errors[0]
    assert (backup / "note.md").read_text() == "vault"


def test_changed_deployment_copy_keeps_failed_record_but_clears_other_records(
    tmp_path, seed_legacy_quarantine
):
    identifier, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    engine, batch_id, target = deploy(tmp_path)
    preview = inspect_transaction_clear(tmp_path / "state", Event())
    deployment_backup = next(target.glob(".obmanage-deploy-*.backup"))
    (deployment_backup / "new-unknown.md").write_text("keep")
    result = clear_transactions(tmp_path / "state", preview, Event())
    assert not result.success and result.removed == 1 and result.total == 2
    assert (deployment_backup / "new-unknown.md").read_text() == "keep"
    assert engine.get_batch(batch_id).status == "resolved"
    assert not backup.exists()
    assert TrashCleanupEngine(tmp_path / "state").list_operations() == ()


def test_cancel_between_transactions_retains_remaining_records(tmp_path, seed_legacy_quarantine, monkeypatch):
    seeds = [make_quarantine(tmp_path, seed_legacy_quarantine, str(i)) for i in range(3)]
    preview = inspect_transaction_clear(tmp_path / "state", Event())
    cancel = Event()
    original = TrashCleanupEngine.clear_transaction

    def clear_one(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(TrashCleanupEngine, "clear_transaction", clear_one)
    result = clear_transactions(tmp_path / "state", preview, cancel)
    assert result.cancelled and result.removed == 1
    assert len(TrashCleanupEngine(tmp_path / "state").list_operations()) == 2


def test_inspection_rejects_unknown_content_without_writes(tmp_path, seed_legacy_quarantine):
    identifier, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    (backup.parent / "unknown.txt").write_text("keep")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(TrashSafetyError, match="未知项目"):
        inspect_transaction_clear(tmp_path / "state", Event())
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_inspection_counts_remaining_copies_and_transaction_directories(tmp_path, seed_legacy_quarantine):
    identifier, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    engine = TrashCleanupEngine(tmp_path / "state")
    preview = engine.inspect_clear(identifier)
    assert (preview.file_count, preview.dir_count, preview.total_bytes) == (1, 2, 5)
    assert engine.finalize(identifier).status == "success"
    preview = engine.inspect_clear(identifier)
    assert (preview.file_count, preview.dir_count, preview.total_bytes) == (0, 0, 0)
