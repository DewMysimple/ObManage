from dataclasses import replace
from threading import Event

import pytest

import obmanage.engine as engine_module
from obmanage.engine import SyncEngine
from obmanage.models import SyncError
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
