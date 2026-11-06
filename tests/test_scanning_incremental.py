"""Collection planning equivalence and changes across task-wide barriers."""
import os
import shutil
import sqlite3
from threading import Event

import pytest

from obmanage.engine import SyncEngine
from obmanage.management.incremental import IncrementalEngine, selected_plans
from obmanage.models import SyncCancelled, SyncError
from obmanage.paths import canonical
from obmanage.scanning.collection import collect_collection
from obmanage.scanning.metrics import ScanMetrics
from obmanage.scanning.session import ScanSession
from tools.benchmark_scanning import BASELINE, reference_package, signature


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


@pytest.mark.parametrize("fault,sides", [
    ("open", ("local",)), ("open", ("local", "portable")),
    ("iteration", ("local", "portable")), ("lstat", ("local",)),
    ("direntry", ("portable",)), ("marker", ("local", "portable")),
])
def test_blocked_discovery_keeps_both_diagnostics_and_skips_comparison(tmp_path, monkeypatch, fault, sides):
    import obmanage.management.incremental as module
    from test_incremental import tree

    roots = {side: tmp_path / side for side in ("local", "portable")}
    for root in roots.values():
        vault(root, "A", root.name.encode())
        vault(root, "B", b"same")
    before = tuple(tree(root) for root in roots.values())
    locations = {str(roots[side] / name / (".obsidian" if fault == "marker" else "notes"))
                 for side in sides for name in ("A", "B")}
    real_collect, real_scan, real_lstat = module.collect_collection, os.scandir, os.lstat
    sessions, handles = [], []

    def collect(root, session, **kwargs):
        sessions.append(session)
        return real_collect(root, session, **kwargs)

    class BrokenEntry:
        name = "x.md"

        def stat(self, **_):
            raise PermissionError("injected entry metadata")

    def enumerate_directory(path):
        if canonical(path) not in locations:
            return real_scan(path)
        if fault in ("open", "marker"):
            raise PermissionError("injected directory access")
        if fault not in ("iteration", "direntry"):
            return real_scan(path)
        from contextlib import contextmanager

        @contextmanager
        def failing_iterator():
            with real_scan(path) as iterator:
                def entries():
                    if fault == "iteration":
                        next(iterator)
                        raise PermissionError("injected directory iteration")
                    yield BrokenEntry()
                try:
                    yield entries()
                finally:
                    handles.append(path)
        return failing_iterator()

    def lstat(path, *args, **kwargs):
        if fault == "lstat" and canonical(path) in {name + os.sep + "x.md" for name in locations}:
            raise PermissionError("injected path metadata")
        return real_lstat(path, *args, **kwargs)

    def forbidden(*_, **__):
        pytest.fail("blocked discovery reached comparison or SQLite")

    engine = IncrementalEngine(tmp_path / "state")
    metrics = ScanMetrics()
    with monkeypatch.context() as injected, metrics.activate():
        injected.setattr(os, "scandir", enumerate_directory)
        injected.setattr(os, "lstat", lstat)
        injected.setattr(module, "collect_collection", collect)
        injected.setattr(module, "BaselineStore", forbidden)
        injected.setattr(SyncEngine, "_analyze", forbidden)
        analysis = engine.analyze(*(str(root) for root in roots.values()))
    assert not analysis.pairs and analysis.identical_count == 0
    for location in locations:
        assert any(location in issue for issue in analysis.issues), analysis.issues
    assert len(sessions) == 2 and sessions[0] is sessions[1] and sessions[0].closed
    if fault in ("iteration", "direntry"):
        assert {canonical(path) for path in handles} == locations
    assert metrics.status == "incomplete" and not metrics.seconds["final_revalidation"]
    with pytest.raises(SyncError, match="扫描不完整"):
        selected_plans(analysis, {"a": "local"})
    assert not engine.execute(analysis, {"a": "local"}).outcomes
    assert not engine.state_dir.exists()
    assert tuple(tree(root) for root in roots.values()) == before
    assert not engine.analyze(*(str(root) for root in roots.values())).issues


def test_pairing_casefold_ambiguity_does_not_open_sqlite(tmp_path):
    local, portable = tmp_path / "local", tmp_path / "portable"
    vault(local, "straße")
    vault(local, "strasse")
    vault(portable, "strasse")
    engine = IncrementalEngine(tmp_path / "state")
    analysis = engine.analyze(str(local), str(portable))
    assert any("对应路径存在歧义" in issue for issue in analysis.issues)
    assert not analysis.pairs and not engine.state_dir.exists()


@pytest.mark.parametrize("deep", [False, True])
def test_bidirectional_plans_and_full_snapshots_match_immutable_reference(tmp_path, deep):
    local, portable = collections(tmp_path)
    with reference_package(BASELINE) as reference:
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
        if not changed and (change != "file" or args[0].source == str(local / "one/同名")):
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
    assert analysis.issues
    assert all(not p.context.get("validated") for pair in analysis.pairs
               for p in (pair.local_plan, pair.portable_plan) if p is not None)
    if change == "file":
        pair = next(p for p in analysis.pairs if p.key == "one/同名")
        assert pair.errors and not pair.local_plan.can_execute


@pytest.mark.parametrize("mutation", [
    "file_add", "file_remove", "file_rename", "dir_add", "dir_remove", "dir_rename",
    "new_vault", "marker_rename", "nested_add", "nested_remove",
    "missing_target_file", "missing_target_vault", "missing_target_junction",
    "directory_replaced", "volume_replaced",
])
def test_live_final_scan_rejects_scope_changes_before_authorizing_either_direction(tmp_path, monkeypatch, mutation):
    import subprocess
    import obmanage.paths as paths
    from test_incremental import tree

    local, portable = collections(tmp_path)
    notes = local / "parent/notes"
    empty = notes / "empty"
    empty.mkdir()
    hidden_vault = local / "parent/unregistered"
    hidden_vault.mkdir()
    missing = portable / "local-only"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.md").write_bytes(b"outside")
    real_volume = paths.volume_identity
    final_started = False
    scans = []
    real_scan = os.scandir

    def volume(path):
        return ("simulated-new-device" if final_started and mutation == "volume_replaced"
                and path == str(local / "parent") else real_volume(path))

    def scan(path):
        if final_started:
            scans.append(canonical(path))
        return real_scan(path)

    def before_final(update):
        nonlocal final_started
        if "正在复核两端集合" not in update.message:
            return
        if mutation == "file_add":
            (notes / "added.md").write_bytes(b"new")
        elif mutation == "file_remove":
            (notes / "x.md").unlink()
        elif mutation == "file_rename":
            (notes / "x.md").rename(notes / "moved.md")
        elif mutation == "dir_add":
            (notes / "added").mkdir()
        elif mutation == "dir_remove":
            empty.rmdir()
        elif mutation == "dir_rename":
            empty.rename(notes / "renamed")
        elif mutation == "new_vault":
            vault(local, "new-discovery/intermediate/vault")
        elif mutation == "marker_rename":
            (local / "one/同名/.obsidian").rename(local / "one/同名/renamed")
        elif mutation == "nested_add":
            (hidden_vault / ".obsidian").mkdir()
        elif mutation == "nested_remove":
            (local / "parent/child/.obsidian").rmdir()
        elif mutation == "missing_target_file":
            missing.write_bytes(b"must keep")
        elif mutation == "missing_target_vault":
            vault(portable, "local-only", b"unrelated vault")
        elif mutation == "missing_target_junction":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(missing), str(outside)],
                                  capture_output=True)
            if made.returncode:
                pytest.skip("Junction unavailable")
        elif mutation == "directory_replaced":
            empty.rename(tmp_path / "original-dir")
            empty.mkdir()
        final_started = True

    if mutation == "missing_target_junction" and os.name != "nt":
        pytest.skip("Windows junction")
    monkeypatch.setattr(paths, "volume_identity", volume)
    monkeypatch.setattr(os, "scandir", scan)
    engine = IncrementalEngine(tmp_path / "state")
    try:
        analysis = engine.analyze(str(local), str(portable), progress=before_final)
        assert final_started and str(local) in scans and str(portable) in scans
        assert analysis.issues
        assert all(not plan.context.get("validated") for pair in analysis.pairs
                   for plan in (pair.local_plan, pair.portable_plan) if plan is not None)
        before = tree(local), tree(portable), tree(outside)
        for side in ("local", "portable"):
            with pytest.raises(SyncError, match="扫描不完整"):
                selected_plans(analysis, {"parent": side})
            assert not engine.execute(analysis, {"parent": side}).outcomes
        assert (tree(local), tree(portable), tree(outside)) == before
    finally:
        if mutation == "missing_target_junction" and missing.is_junction():
            missing.rmdir()


def test_shared_final_manifest_comparison_still_revalidates_both_root_bindings(tmp_path, monkeypatch):
    import obmanage.management.incremental as module
    from obmanage.scanning.session import ManifestView

    local, portable = collections(tmp_path)
    final_started = False
    compared, roots = [], []
    compare, revalidate = module._require_manifest, module.revalidate_roots
    dictionaries = ManifestView.dictionaries

    def progress(update):
        nonlocal final_started
        if "正在复核两端集合" in update.message:
            final_started = True

    def manifest(expected, current, **kwargs):
        compared.append(kwargs["root"])
        return compare(expected, current, **kwargs)

    def check_roots(context):
        roots.append((context["source"], context["target"]))
        assert not context.get("validated")
        return revalidate(context)

    def materialize(view):
        assert not final_started
        return dictionaries(view)

    monkeypatch.setattr(module, "_require_manifest", manifest)
    monkeypatch.setattr(module, "revalidate_roots", check_roots)
    monkeypatch.setattr(ManifestView, "dictionaries", materialize)
    analysis = IncrementalEngine(tmp_path / "state").analyze(str(local), str(portable), progress=progress)
    assert not analysis.issues and final_started
    assert compared.count(str(local / "parent")) == compared.count(str(portable / "parent")) == 1
    assert (str(local / "parent"), str(portable / "parent")) in roots
    assert (str(portable / "parent"), str(local / "parent")) in roots
    assert roots[-1] == (str(local), str(portable))
    pair = next(pair for pair in analysis.pairs if pair.key == "parent")
    for side in ("local", "portable"):
        plan = pair.plan_for(side)
        assert plan.context.pop("validated") is True
        with pytest.raises(SyncError, match="没有可执行"):
            selected_plans(analysis, {"parent": side})


def test_reverse_plan_with_a_distinct_expectation_cannot_borrow_validation(tmp_path, monkeypatch):
    local, portable = collections(tmp_path)
    reverse = SyncEngine._reverse_plan

    def reverse_with_change(self, *args, **kwargs):
        forward, backward = reverse(self, *args, **kwargs)
        if forward.source == str(local / "parent"):
            entries = dict(backward.context["source_entries"])
            entries.pop("notes/x.md")
            backward.context["source_entries"] = entries
        return forward, backward

    monkeypatch.setattr(SyncEngine, "_reverse_plan", reverse_with_change)
    analysis = IncrementalEngine(tmp_path / "state").analyze(str(local), str(portable))
    assert analysis.issues
    parent = next(pair for pair in analysis.pairs if pair.key == "parent")
    assert "notes/x.md" in parent.portable_plan.errors[0]
    assert not parent.local_plan.context.get("validated")
    assert not parent.portable_plan.context.get("validated")


@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError, SyncCancelled])
def test_final_scan_fault_closes_session_and_sqlite_and_cannot_publish_plans(tmp_path, monkeypatch, error):
    import obmanage.management.incremental as module
    from test_incremental import tree

    local, portable = collections(tmp_path)
    before = tree(local), tree(portable)
    engine = IncrementalEngine(tmp_path / "state")
    real_collect, real_scan, real_close = module.collect_collection, os.scandir, module.BaselineStore.close
    final_started = False
    sessions, stores = [], []
    cancel = Event()

    def progress(update):
        nonlocal final_started
        if "正在复核两端集合" in update.message:
            final_started = True

    def collect(root, session, **kwargs):
        sessions.append(session)
        return real_collect(root, session, **kwargs)

    def scan(path):
        if final_started and canonical(path) == str(portable / "parent/notes"):
            if error == SyncCancelled:
                cancel.set()
            else:
                raise error("injected final observation fault")
        return real_scan(path)

    def close(store):
        real_close(store)
        stores.append(store)

    with monkeypatch.context() as injected:
        injected.setattr(module, "collect_collection", collect)
        injected.setattr(module.BaselineStore, "close", close)
        injected.setattr(os, "scandir", scan)
        if error == SyncCancelled:
            with pytest.raises(SyncCancelled):
                engine.analyze(str(local), str(portable), progress=progress, cancel=cancel)
        else:
            analysis = engine.analyze(str(local), str(portable), progress=progress)
            assert any("injected final observation fault" in issue for issue in analysis.issues)
            assert all(not p.context.get("validated") for pair in analysis.pairs
                       for p in (pair.local_plan, pair.portable_plan) if p is not None)
    assert final_started and sessions[0] is not sessions[-1]
    assert all(session.closed for session in sessions)
    assert len(stores) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        stores[0].connection.execute("SELECT 1")
    assert (tree(local), tree(portable)) == before
    assert not engine.analyze(str(local), str(portable)).issues


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
