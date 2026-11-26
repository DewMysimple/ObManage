from dataclasses import replace
from threading import Event

import pytest

import obmanage.engine as engine_module
import obmanage.sync_workflow as workflow
from obmanage.engine import SyncEngine
from obmanage.models import SyncError
from obmanage.paths import identity, snapshot
from obmanage.scanning.metrics import ScanMetrics
from obmanage.store import BaselineStore
from obmanage.sync_workflow import SyncIntent, SyncRequest, preview_copy, run_sync


@pytest.fixture
def roots(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "note.md").write_bytes(b"new")
    (target / "old.md").write_bytes(b"old")
    request = SyncRequest(str(source), str(target), "来源", "目标", "mirror", SyncIntent.MANUAL)
    return SyncEngine(tmp_path / "state"), request, source, target


@pytest.mark.parametrize("mode", ["mirror", "no_video"])
def test_direct_confirmation_has_one_inventory_per_side_and_rejects_later_changes(roots, monkeypatch, mode):
    engine, request, source, target = roots
    scans = []
    original = engine_module._scan

    def scan(root, *args, **kwargs):
        scans.append(root)
        return original(root, *args, **kwargs)

    def confirm(plan):
        assert scans == [str(source), str(target)]
        assert plan.counts["add"] == plan.counts["delete"] == 1
        assert not (target / "note.md").exists()
        (target / "arrived.md").write_bytes(b"keep")
        return True

    monkeypatch.setattr(engine_module, "_scan", scan)
    result = run_sync(engine, replace(request, mode=mode), Event(), None, confirm=confirm)
    assert result.status == "failed" and result.preflight_failed
    assert scans == [str(source), str(target)] * 2
    assert {p.name for p in target.iterdir()} == {"arrived.md", "old.md"}


@pytest.mark.parametrize("mismatch", ["direction", "mode", "display", "scheduled", "deep"])
def test_review_cannot_transfer_to_a_different_request_or_display_copy(roots, mismatch):
    engine, request, source, target = roots
    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    changes = {
        "direction": dict(source=request.target, target=request.source),
        "mode": dict(mode="no_video"),
        "display": dict(reviewed_plan=preview_copy(plan)),
        "scheduled": dict(intent=SyncIntent.SCHEDULED),
        "deep": dict(deep=True),
    }
    reviewed = replace(replace(request, reviewed_plan=plan), **changes[mismatch])
    with pytest.raises(SyncError):
        run_sync(engine, reviewed, Event(), None)
    assert {p.name for p in source.iterdir()} == {"note.md"}
    assert {p.name for p in target.iterdir()} == {"old.md"}


def test_reviewed_noop_is_checked_again_before_reporting_no_changes(roots):
    engine, request, source, target = roots
    (target / "old.md").unlink()
    (target / "note.md").write_bytes((source / "note.md").read_bytes())
    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    assert not plan.has_changes
    (target / "new.md").write_bytes(b"keep")
    with pytest.raises(SyncError, match="预览后发生变化"):
        run_sync(engine, replace(request, reviewed_plan=plan), Event(), None)
    assert (target / "new.md").exists()


def test_candidate_without_end_check_is_bound_to_preparing_engine(roots):
    engine, request, _source, target = roots
    plan = engine.analyze(request.source, request.target, for_execution=True)
    assert not plan.context.get("validated")
    result = SyncEngine(engine.state_dir).execute(plan)
    assert result.preflight_failed and (target / "old.md").exists()
    assert engine.execute(plan).status == "success"


@pytest.mark.parametrize("mode", ["mirror", "no_video"])
@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize(("copy", "delete", "execution_pairs"), [
    (True, True, 3), (True, False, 2), (False, True, 2), (False, False, 1),
])
def test_review_inventory_reads_by_endpoint(roots, monkeypatch, mode, deep, copy, delete, execution_pairs):
    engine, request, source, target = roots
    request = replace(request, mode=mode)
    for side in (source, target):
        (side / "keep.md").write_bytes(b"identical")
    if not copy:
        (source / "note.md").unlink()
    if not delete:
        (target / "old.md").unlink()
    assert engine.analyze(str(source), str(target), mode=mode).can_execute  # Warm the exact hash baseline.
    scans = []
    original = engine_module._scan

    def trace(root, *args, **kwargs):
        scans.append(root)
        return original(root, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_scan", trace)
    metrics = ScanMetrics()
    with metrics.activate():
        plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW, deep=deep), Event(), None)
        ends = [str(source), str(target)]
        assert scans == ends * (2 if deep else 1)
        assert bool(plan.context.get("validated")) == deep
        assert (target / "old.md").exists() == delete
        assert not (target / "note.md").exists()
        scans.clear()
        result = run_sync(engine, replace(request, reviewed_plan=plan), Event(), None)
    assert scans == ends * execution_pairs
    assert metrics.counts["predelete_reused_preflight"] == int(delete and not copy)
    if copy or delete:
        assert result.status == "success", result.errors
    else:
        assert not result.has_changes
    assert not (target / "old.md").exists()
    assert (target / "note.md").exists() == copy
    assert (target / "keep.md").read_bytes() == b"identical"


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("side", ["source", "target"])
def test_change_during_comparison_never_enters_a_reviewed_write(roots, side, deep):
    engine, request, source, target = roots
    arrived = (source if side == "source" else target) / "arrived.md"

    def change(event):
        if event.phase == "compare" and not arrived.exists():
            arrived.write_bytes(b"from another application")

    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW, deep=deep), Event(), change)
    if deep:
        assert not plan.can_execute and not engine.prepared(plan)
    else:
        assert engine.prepared(plan) and not plan.context.get("validated")
        result = run_sync(engine, replace(request, reviewed_plan=plan), Event(), None)
        assert result.preflight_failed and result.status == "failed"
        assert not engine.prepared(plan)
    assert arrived.read_bytes() == b"from another application"
    assert (target / "old.md").exists() and not (target / "note.md").exists()


def test_quick_preview_cannot_move_to_another_engine_or_survive_a_new_analysis(roots):
    engine, request, _source, target = roots
    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    reviewed = replace(request, reviewed_plan=plan)
    with pytest.raises(SyncError, match="预览与本轮同步不匹配"):
        run_sync(SyncEngine(engine.state_dir), reviewed, Event(), None)
    run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    with pytest.raises(SyncError, match="预览与本轮同步不匹配"):
        run_sync(engine, reviewed, Event(), None)
    assert (target / "old.md").exists()


def test_unchanged_path_spelling_checks_replaced_root_at_preflight(roots, monkeypatch):
    engine, request, source, target = roots
    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    monkeypatch.setattr(workflow, "validate_roots", lambda *_: pytest.fail("duplicate root binding"))
    original = target.rename(target.with_name("prior-target"))
    target.mkdir()
    (target / "old.md").write_bytes(b"replacement")
    result = run_sync(engine, replace(request, reviewed_plan=plan), Event(), None)
    assert result.preflight_failed and "目标目录已被替换" in result.errors[0]
    assert (target / "old.md").read_bytes() == b"replacement"
    assert (original / "old.md").exists() and (source / "note.md").exists()


def test_alternate_spelling_is_resolved_before_reusing_review(roots, monkeypatch):
    engine, request, _source, target = roots
    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    calls = []
    original = workflow.validate_roots

    def resolve(source, destination):
        calls.append(destination)
        return original(source, destination)

    monkeypatch.setattr(workflow, "validate_roots", resolve)
    spelling = str(target) + "/."
    result = run_sync(engine, replace(request, target=spelling, reviewed_plan=plan), Event(), None)
    assert result.status == "success" and calls == [spelling]


def test_delete_only_samples_both_sides_after_local_preparation(roots, monkeypatch):
    engine, request, source, target = roots
    (target / "note.md").write_bytes((source / "note.md").read_bytes())
    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    original = BaselineStore.temps

    def open_state(store, pair):
        (source / "late.md").write_bytes(b"arrived while opening the database")
        return original(store, pair)

    monkeypatch.setattr(BaselineStore, "temps", open_state)
    result = run_sync(engine, replace(request, reviewed_plan=plan), Event(), None)
    assert result.status == "failed" and result.preflight_failed and result.deleted_files == 0
    assert (target / "old.md").exists()


def test_recovered_temp_forces_new_inventory_before_other_deletions(roots):
    engine, request, source, target = roots
    (target / "note.md").write_bytes((source / "note.md").read_bytes())
    owned = target / ".obmanage-interrupted.tmp"
    owned.write_bytes(b"partial")
    plan = run_sync(engine, replace(request, intent=SyncIntent.PREVIEW), Event(), None)
    store = BaselineStore(engine.state_dir)
    try:
        store.register_temp(plan.pair_id, str(owned), identity(snapshot(owned)))
    finally:
        store.close()

    def change(event):
        if event.phase == "delete" and event.relative_path == owned.name:
            (source / "note.md").write_bytes(b"edited during recovery")

    result = run_sync(engine, replace(request, reviewed_plan=plan), Event(), change)
    assert result.status == "failed" and result.deleted_files == 1 and not result.preflight_failed
    assert not owned.exists() and (target / "old.md").exists()
