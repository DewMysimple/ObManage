"""Collection planning equivalence and changes across task-wide barriers."""
import os
import shutil
import sqlite3
from threading import Event

import pytest

from obmanage.engine import SyncEngine
from obmanage.management.incremental import IncrementalEngine
from obmanage.models import SyncCancelled, SyncError
from obmanage.scanning.collection import collect_collection
from obmanage.scanning.metrics import ScanMetrics
from obmanage.scanning.session import ScanSession
from tools.benchmark_scanning import reference_package, signature


def vault(root, relative, data=b"same"):
    path = root / relative
    (path / ".obsidian").mkdir(parents=True, exist_ok=True)
    (path / "notes").mkdir(exist_ok=True)
    (path / "notes/x.md").write_bytes(data)
    return path


def collections(tmp_path):
    local, portable = tmp_path / "电脑", tmp_path / "移动盘"
    for root in (local, portable):
        vault(root, "parent")
        vault(root, "parent/child")
        vault(root, "two/同名")
        vault(root, "one/同名")
    (portable / "parent/notes/obsolete.md").write_bytes(b"delete")
    (local / "parent/child/notes/x.md").write_bytes(b"diff")
    vault(local, "local-only")
    vault(portable, "portable-only")
    return local, portable


@pytest.mark.parametrize("deep", [False, True])
def test_bidirectional_plans_and_full_snapshots_match_immutable_reference(tmp_path, deep):
    local, portable = collections(tmp_path)
    with reference_package() as reference:
        old_engine = reference["management.incremental"].IncrementalEngine(tmp_path / "old-state")
        new_engine = IncrementalEngine(tmp_path / "new-state")
        for _ in range(2):  # Both first adoption and exact-baseline reuse.
            old = old_engine.analyze(str(local), str(portable), deep=deep)
            new = new_engine.analyze(str(local), str(portable), deep=deep)
            assert signature(old) == signature(new)
            assert not new.issues
            assert all(p.context["validated"] for pair in new.pairs
                       for p in (pair.local_plan, pair.portable_plan) if p is not None)


def test_direct_vault_roots_and_existing_non_vault_target_match_reference(tmp_path):
    local, portable = collections(tmp_path)
    with reference_package() as reference:
        old = reference["management.incremental"].IncrementalEngine(tmp_path / "old")
        new = IncrementalEngine(tmp_path / "new")
        assert signature(old.analyze(str(local / "parent"), str(portable / "parent"))) == signature(
            new.analyze(str(local / "parent"), str(portable / "parent")))
        (portable / "local-only").mkdir()
        assert signature(old.analyze(str(local), str(portable))) == signature(new.analyze(str(local), str(portable)))


def test_nested_initial_manifest_is_enumerated_once_and_contains_child_configuration(tmp_path, monkeypatch):
    from obmanage.scanning import io
    local, _ = collections(tmp_path)
    original = os.scandir
    calls = []
    def record(path):
        from obmanage.paths import canonical
        calls.append(canonical(path))
        return original(path)
    monkeypatch.setattr(os, "scandir", record)
    with ScanSession(capture=True) as session:
        collection = collect_collection(local, session)
        parent = collection.view(local / "parent")
        child = collection.view(local / "parent/child")
        assert "child/.obsidian" in parent.entries
        assert "child/notes/x.md" in parent.entries
        assert parent.entries["child/notes/x.md"] == child.entries["notes/x.md"]
    assert calls.count(str(local / "parent/child/notes")) == 1


@pytest.mark.parametrize("change", ["file", "new_vault", "marker", "target_directory"])
def test_task_final_observation_rejects_changes_after_comparison(tmp_path, monkeypatch, change):
    local, portable = collections(tmp_path)
    original = SyncEngine._plan_from_manifests
    changed = False
    def compare(self, *args, **kwargs):
        nonlocal changed
        result = original(self, *args, **kwargs)
        if not changed:
            changed = True
            if change == "file":
                (local / "one/同名/notes/x.md").write_bytes(b"changed later")
            elif change == "new_vault":
                vault(local, "newly-created")
            elif change == "marker":
                (local / "two/同名/.obsidian").rmdir()
            else:
                (portable / "local-only").mkdir()
        return result
    monkeypatch.setattr(SyncEngine, "_plan_from_manifests", compare)
    engine = IncrementalEngine(tmp_path / "state")
    analysis = engine.analyze(str(local), str(portable))
    assert changed
    assert analysis.issues or any(pair.errors for pair in analysis.pairs)
    if change in ("new_vault", "marker"):
        assert analysis.issues  # Pairing and nested boundaries invalidate the collection.
    if change == "file":
        pair = next(p for p in analysis.pairs if p.key == "one/同名")
        assert pair.errors and not pair.local_plan.can_execute


@pytest.mark.parametrize("stage", ["discovery", "hash", "final"])
def test_cancellation_at_each_stage_closes_shared_store_and_releases_task_slot(tmp_path, monkeypatch, stage):
    import obmanage.management.incremental as module
    local, portable = collections(tmp_path)
    cancel = Event()
    real_close = module.BaselineStore.close
    closed = []
    def close(self):
        closed.append(self)
        return real_close(self)
    monkeypatch.setattr(module.BaselineStore, "close", close)
    def progress(event):
        if ((stage == "discovery" and event.phase == "catalog")
                or (stage == "hash" and event.total_bytes)
                or (stage == "final" and "正在复核两端集合" in event.message)):
            cancel.set()
    engine = IncrementalEngine(tmp_path / "state")
    with pytest.raises(SyncCancelled):
        engine.analyze(str(local), str(portable), progress=progress, cancel=cancel)
    assert len(closed) == (0 if stage == "discovery" else 1)
    cancel.clear()
    assert not engine.analyze(str(local), str(portable), cancel=cancel).issues


def test_shared_sqlite_connection_closes_after_error_and_does_not_hold_the_task_lock(tmp_path, monkeypatch):
    import obmanage.management.incremental as module
    local, portable = collections(tmp_path)
    original = module.BaselineStore.records
    calls = 0
    def fail_once(self, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("injected")
        return original(self, *args)
    monkeypatch.setattr(module.BaselineStore, "records", fail_once)
    metrics = ScanMetrics()
    with metrics.activate():
        analysis = IncrementalEngine(tmp_path / "state").analyze(str(local), str(portable))
    assert metrics.counts["sqlite_connections"] == 1
    assert any(pair.errors for pair in analysis.pairs)
    assert not IncrementalEngine(tmp_path / "state").analyze(str(local), str(portable)).issues


def test_full_scan_still_reads_content_with_preserved_size_and_mtime(tmp_path):
    local, portable = collections(tmp_path)
    engine = IncrementalEngine(tmp_path / "state")
    engine.analyze(str(local), str(portable))
    changed = local / "one/同名/notes/x.md"
    state = changed.stat()
    changed.write_bytes(b"diff")
    os.utime(changed, ns=(state.st_atime_ns, state.st_mtime_ns))
    result = engine.analyze(str(local), str(portable), deep=True)
    pair = next(p for p in result.pairs if p.key == "one/同名")
    assert pair.local_plan.counts["update"] == pair.portable_plan.counts["update"] == 1


def test_different_nesting_relationships_match_reference(tmp_path):
    local, portable = collections(tmp_path)
    (local / "parent/child/.obsidian").rmdir()
    with reference_package() as reference:
        old = reference["management.incremental"].IncrementalEngine(tmp_path / "old")
        new = IncrementalEngine(tmp_path / "new")
        assert signature(old.analyze(str(local), str(portable))) == signature(new.analyze(str(local), str(portable)))


def test_close_error_still_releases_incremental_and_mirror_locks(tmp_path, monkeypatch):
    import obmanage.store as store
    local, portable = collections(tmp_path)
    original = store.BaselineStore.close
    def fail(self):
        original(self)
        raise sqlite3.OperationalError("close injected")
    with monkeypatch.context() as patch:
        patch.setattr(store.BaselineStore, "close", fail)
        with pytest.raises(sqlite3.Error):
            IncrementalEngine(tmp_path / "state").analyze(str(local), str(portable))
        with pytest.raises(sqlite3.Error):
            SyncEngine(tmp_path / "mirror-state").analyze(str(local / "parent"), str(portable / "parent"))
    assert not IncrementalEngine(tmp_path / "state").analyze(str(local), str(portable)).issues
    assert SyncEngine(tmp_path / "mirror-state").analyze(str(local / "parent"), str(portable / "parent")).can_execute


def test_sqlite_initialization_error_closes_the_new_connection(tmp_path, monkeypatch):
    import obmanage.store as module
    closed = []
    class Connection:
        def execute(self, *_): raise sqlite3.DatabaseError("initialization injected")
        def close(self): closed.append(True)
    monkeypatch.setattr(module.sqlite3, "connect", lambda *args, **kwargs: Connection())
    with pytest.raises(sqlite3.DatabaseError): module.BaselineStore(tmp_path / "state")
    assert closed == [True]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_state_directory_replacement_during_discovery_cannot_write_a_database_in_a_vault(tmp_path):
    import _winapi
    local, portable = collections(tmp_path)
    state = tmp_path / "state"
    changed = False
    def progress(event):
        nonlocal changed
        if event.phase == "catalog" and not changed:
            _winapi.CreateJunction(str(local), str(state))
            changed = True
    with pytest.raises(SyncError):
        IncrementalEngine(state).analyze(str(local), str(portable), progress=progress)
    assert changed
    assert not (local / "baselines.sqlite3").exists()
    assert not (portable / "baselines.sqlite3").exists()
