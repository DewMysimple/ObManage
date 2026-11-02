from threading import Event

import pytest

from obmanage.management.deployment import (
    DeploymentComponent, DeploymentEngine, DeploymentRequest, DeploymentResult,
    DeploymentSelection, DeploymentTarget,
)
from obmanage.management.transactions import (
    clear_transactions, complete_transactions, inspect_transaction_clear,
    inspect_transaction_completion,
)
from obmanage.management.trash import TrashCleanupEngine, TrashSafetyError
from obmanage.models import SyncError


def make_quarantine(root, seed, name="vault"):
    state, vault = root / "state", root / name
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash/note.md").write_text(name)
    identifier, backup = seed(state, vault)
    return identifier, backup, vault


def deploy(root, *, end=True, name=""):
    suffix = f"-{name}" if name else ""
    source, target = (root / (part + suffix) for part in ("source", "target"))
    state = root / "state"
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


@pytest.mark.parametrize("inspect", [inspect_transaction_clear, inspect_transaction_completion])
def test_inspection_rejects_unknown_content_without_writes(tmp_path, seed_legacy_quarantine, inspect):
    identifier, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    (backup.parent / "unknown.txt").write_text("keep")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(TrashSafetyError, match="未知项目"):
        inspect(tmp_path / "state", Event())
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_inspection_counts_remaining_copies_and_transaction_directories(tmp_path, seed_legacy_quarantine):
    identifier, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    engine = TrashCleanupEngine(tmp_path / "state")
    preview = engine.inspect_clear(identifier)
    assert (preview.file_count, preview.dir_count, preview.total_bytes) == (1, 2, 5)
    assert engine.finalize(identifier).status == "success"
    preview = engine.inspect_clear(identifier)
    assert (preview.file_count, preview.dir_count, preview.total_bytes) == (0, 0, 0)


def test_complete_pending_transaction_keeps_current_content_and_removes_copies_and_record(tmp_path):
    engine, batch_id, target = deploy(tmp_path, end=False)
    (target / ".obsidian/config.json").write_text("work after synchronization")
    recovery = engine.inspect_recovery(batch_id)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    preview = inspect_transaction_completion(tmp_path / "state", Event(), recovery)

    assert preview.count == 1 and preview.clear_preview.pending_deployments == 0
    assert preview.selected is recovery
    assert batch_id in preview.details
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    result = complete_transactions(tmp_path / "state", preview, Event())

    assert result.success and result.removed == 1
    assert engine.list_batches() == ()
    assert not tuple(target.glob(".obmanage-deploy-*"))
    assert (target / ".obsidian/config.json").read_text() == "work after synchronization"
    assert (tmp_path / "source/.obsidian/config.json").read_text() == "new"


def test_complete_selected_and_all_ended_and_quarantine_transactions(
        tmp_path, seed_legacy_quarantine):
    _, backup, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    deploy(tmp_path, name="ended")
    engine, batch_id, target = deploy(tmp_path, end=False, name="pending")
    preview = inspect_transaction_completion(
        tmp_path / "state", Event(), engine.inspect_recovery(batch_id)
    )

    assert preview.count == 3
    result = complete_transactions(tmp_path / "state", preview, Event())

    assert result.success and result.removed == 3
    assert engine.list_batches() == ()
    assert TrashCleanupEngine(tmp_path / "state").list_operations() == ()
    assert not backup.exists()
    assert (target / ".obsidian/config.json").read_text() == "new"


def test_complete_does_not_adopt_pending_deployment_created_after_preview(tmp_path):
    engine, old_id, _ = deploy(tmp_path, name="ended")
    preview = inspect_transaction_completion(tmp_path / "state", Event())
    _, new_id, target = deploy(tmp_path, name="new-pending", end=False)

    result = complete_transactions(tmp_path / "state", preview, Event())

    assert result.success and result.removed == 1
    assert old_id != new_id
    assert [batch.batch_id for batch in engine.list_batches()] == [new_id]
    assert engine.get_batch(new_id).status == "committed"
    assert tuple(target.glob(".obmanage-deploy-*.backup"))


@pytest.mark.parametrize("condition", ["stale", "cancelled"])
def test_complete_stale_selected_or_cancelled_writes_nothing(
        tmp_path, seed_legacy_quarantine, condition):
    make_quarantine(tmp_path, seed_legacy_quarantine)
    engine, batch_id, _ = deploy(tmp_path, end=False)
    preview = inspect_transaction_completion(
        tmp_path / "state", Event(), engine.inspect_recovery(batch_id)
    )
    cancel = Event()
    if condition == "stale":
        engine.journal.set_batch(batch_id, error="changed after preview")
    else:
        cancel.set()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    result = complete_transactions(tmp_path / "state", preview, cancel)

    assert not result.success and result.removed == 0
    assert result.cancelled == (condition == "cancelled")
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert engine.get_batch(batch_id).status == "committed"


def test_complete_retains_ended_record_on_cleanup_failure_and_allows_retry(tmp_path, monkeypatch):
    from obmanage.management import deployment

    engine, batch_id, target = deploy(tmp_path, end=False)
    preview = inspect_transaction_completion(
        tmp_path / "state", Event(), engine.inspect_recovery(batch_id)
    )
    original = deployment._remove_owned_tree

    def fail_cleanup(*args, **kwargs):
        raise OSError("injected deletion failure")

    monkeypatch.setattr(deployment, "_remove_owned_tree", fail_cleanup)
    result = complete_transactions(tmp_path / "state", preview, Event())

    assert not result.success and result.removed == 0
    assert "injected deletion failure" in result.errors[0]
    assert engine.get_batch(batch_id).status == "resolved"
    assert tuple(target.glob(".obmanage-deploy-*.backup"))
    monkeypatch.setattr(deployment, "_remove_owned_tree", original)
    retry = inspect_transaction_completion(tmp_path / "state", Event())
    assert retry.selected is None
    assert complete_transactions(tmp_path / "state", retry, Event()).success
    assert engine.list_batches() == ()


@pytest.mark.parametrize("when", ["before-preview", "after-preview"])
def test_complete_retains_unknown_backup_content_but_clears_other_transactions(
        tmp_path, seed_legacy_quarantine, when):
    _, quarantine, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    engine, batch_id, target = deploy(tmp_path, end=False)
    backup = next(target.glob(".obmanage-deploy-*.backup"))
    unknown = backup / "unknown.md"
    if when == "before-preview":
        unknown.write_text("keep")
    recovery = engine.inspect_recovery(batch_id)
    preview = inspect_transaction_completion(tmp_path / "state", Event(), recovery)
    if when == "after-preview":
        unknown.write_text("keep")

    result = complete_transactions(tmp_path / "state", preview, Event())

    assert not result.success and result.removed == 1 and result.total == 2
    assert engine.get_batch(batch_id).status == "resolved"
    assert unknown.read_text() == "keep"
    assert (backup / "config.json").read_text() == "old"
    assert not quarantine.exists()


def test_completion_inspection_rejects_stale_selected_without_writes(tmp_path):
    engine, batch_id, _ = deploy(tmp_path, end=False)
    recovery = engine.inspect_recovery(batch_id)
    engine.journal.set_batch(batch_id, error="changed after recovery inspection")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    with pytest.raises(SyncError, match="检查后已改变"):
        inspect_transaction_completion(tmp_path / "state", Event(), recovery)

    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_complete_terminal_selection_uses_ordinary_clear_preview(tmp_path):
    engine, batch_id, _ = deploy(tmp_path)
    preview = inspect_transaction_completion(
        tmp_path / "state", Event(), engine.inspect_recovery(batch_id)
    )

    assert preview.selected is None and preview.count == 1
    assert complete_transactions(tmp_path / "state", preview, Event()).success
    assert engine.list_batches() == ()


def test_failed_resolve_stops_completion_before_clearing_other_records(
        tmp_path, seed_legacy_quarantine, monkeypatch):
    make_quarantine(tmp_path, seed_legacy_quarantine)
    engine, batch_id, _ = deploy(tmp_path, end=False)
    preview = inspect_transaction_completion(
        tmp_path / "state", Event(), engine.inspect_recovery(batch_id)
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(DeploymentEngine, "resolve", lambda self, identifier, **kwargs:
                        DeploymentResult(identifier, "failed", errors=("cannot resolve",)))

    result = complete_transactions(tmp_path / "state", preview, Event())

    assert not result.success and result.removed == 0
    assert result.errors == ("cannot resolve",)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_complete_does_not_adopt_a_later_revision_after_resolve(
        tmp_path, seed_legacy_quarantine, monkeypatch):
    _, quarantine, _ = make_quarantine(tmp_path, seed_legacy_quarantine)
    engine, batch_id, target = deploy(tmp_path, end=False)
    preview = inspect_transaction_completion(
        tmp_path / "state", Event(), engine.inspect_recovery(batch_id)
    )
    original = DeploymentEngine.resolve

    def resolve_then_change(self, identifier, **kwargs):
        result = original(self, identifier, **kwargs)
        assert result.success
        self.journal.set_batch(identifier, error="later change after resolve")
        return result

    monkeypatch.setattr(DeploymentEngine, "resolve", resolve_then_change)
    result = complete_transactions(tmp_path / "state", preview, Event())

    assert not result.success and result.removed == 1 and result.total == 2
    assert engine.get_batch(batch_id).status == "resolved"
    assert tuple(target.glob(".obmanage-deploy-*.backup"))
    assert not quarantine.exists()


def test_bad_ended_copy_does_not_block_completing_another_pending_deployment(tmp_path):
    from obmanage.management.recovery import require_recovery_clear

    engine, old_id, old_target = deploy(tmp_path, name="ended")
    old_backup = next(old_target.glob(".obmanage-deploy-*.backup"))
    unknown = old_backup / "unknown.md"
    unknown.write_text("keep")
    _, selected_id, selected_target = deploy(tmp_path, name="pending", end=False)
    recovery = engine.inspect_recovery(selected_id)

    with pytest.raises(SyncError, match="无法清理"):
        inspect_transaction_clear(tmp_path / "state", Event())
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = inspect_transaction_completion(tmp_path / "state", Event(), recovery)

    assert preview.count == 2 and preview.clear_preview.pending_deployments == 0
    assert next(item for item in preview.clear_preview.deployments
                if item.batch_id == old_id).cleanup_error
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    result = complete_transactions(tmp_path / "state", preview, Event())

    assert not result.success and result.removed == 1 and result.total == 2
    assert [batch.batch_id for batch in engine.list_batches()] == [old_id]
    assert engine.get_batch(old_id).status == "resolved"
    assert unknown.read_text() == "keep"
    assert (old_backup / "config.json").read_text() == "old"
    assert (selected_target / ".obsidian/config.json").read_text() == "new"
    assert not tuple(selected_target.glob(".obmanage-deploy-*"))
    require_recovery_clear(tmp_path / "state")
