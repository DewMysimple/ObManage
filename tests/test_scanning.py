"""Task epoch, identity, incomplete-result and resource-lifetime contracts."""
import os
from threading import Event

import pytest

from obmanage.models import SyncCancelled, SyncError
from obmanage.paths import canonical, native, snapshot
from obmanage.scanning.metrics import ScanMetrics, count, measured, phase
from obmanage.scanning.session import ScanSession


def test_metrics_are_optional_nested_and_aggregate_without_paths():
    count("ignored")
    metrics = ScanMetrics("test")
    @measured("phase")
    def operation(): count("items", 3)
    with metrics.activate(): operation()
    assert metrics.status == "success"
    assert metrics.counts == {"items": 3}
    assert metrics.seconds["total"] >= metrics.seconds["phase"] >= 0
    assert set(metrics.report()) == {"task", "status", "counts", "seconds"}


def test_session_reuses_directory_observations_but_fresh_epoch_reads_changes(tmp_path):
    (tmp_path / "note.md").write_bytes(b"old")
    metrics = ScanMetrics()
    with metrics.activate(), ScanSession() as session:
        first = session.manifest(tmp_path).require_complete()
        assert session.manifest(tmp_path).entries == first.entries
        assert metrics.counts["reused_directories"] == 1
        assert first.entries["note.md"]["inode"] == (tmp_path / "note.md").stat().st_ino
        (tmp_path / "new.md").write_bytes(b"new")
        with ScanSession() as fresh:
            second = fresh.manifest(tmp_path).require_complete()
            assert first.epoch != second.epoch
            assert first.entries != second.entries
    assert session.closed and session.directories == {}
    with pytest.raises(RuntimeError): session.observe(tmp_path)


def test_manifest_policy_is_explicit_and_projection_never_loses_nested_content(tmp_path):
    for name in (".obsidian", ".trash", "nested/.obsidian", "empty", ".obmanage-deploy-demo/backup"):
        (tmp_path / name).mkdir(parents=True)
    for name in ("note.md", "clip.MP4", "nested/x.md", ".trash/gone.md"):
        (tmp_path / name).write_bytes(b"test")
    with ScanSession() as session:
        mirror = session.manifest(tmp_path)
        backup = session.manifest(tmp_path, policy="no_video")
        archive = session.manifest(tmp_path, policy="archive", exclude_videos=True)
    assert {".obsidian", ".trash/gone.md", "nested/x.md", "empty", "clip.MP4"} <= mirror.entries.keys()
    assert "clip.MP4" not in backup.entries and "clip.MP4" in backup.excluded
    assert ".obmanage-deploy-demo" not in mirror.entries
    assert ".obmanage-deploy-demo" in archive.excluded


@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError, OSError])
def test_scan_failure_cannot_turn_into_an_executable_empty_manifest(tmp_path, monkeypatch, error):
    import obmanage.scanning.session as implementation
    def denied(*args, **kwargs): raise error("injected")
    monkeypatch.setattr(implementation, "scandir", denied)
    with ScanSession() as session:
        result = session.manifest(tmp_path)
        assert not result.complete and result.issues
        with pytest.raises(SyncError): result.dictionaries()


def test_cancellation_closes_session_and_reports_cancelled_metrics(tmp_path):
    cancel = Event()
    metrics = ScanMetrics()
    with pytest.raises(SyncCancelled), metrics.activate(), ScanSession(cancel) as session:
        cancel.set()
        session.manifest(tmp_path)
    assert metrics.status == "cancelled" and session.closed


def test_directory_change_while_enumerating_is_explicit(tmp_path):
    original = __import__("obmanage.paths", fromlist=["snapshot"]).snapshot
    visits = 0
    def change(path):
        nonlocal visits
        state = original(path)
        if canonical(path) == canonical(tmp_path):
            visits += 1
            if visits == 2:
                state = {**state, "inode": state["inode"] + 1}
        return state
    with ScanSession() as session:
        observed = session.observe(tmp_path, snapshotter=change)
        assert observed.issues[0].code == "directory_changed"
        assert "枚举期间" in observed.issues[0].message


def test_observe_reuses_only_the_adjacent_checked_leaf(tmp_path, monkeypatch):
    from obmanage import paths

    checked, enumerated = [], []
    real_lstat, real_scan = os.lstat, os.scandir

    def lstat(path, *args, **kwargs):
        checked.append(canonical(path))
        return real_lstat(path, *args, **kwargs)

    def scandir(path):
        enumerated.append(len(checked))
        return real_scan(path)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "scandir", scandir)
    if paths._get_file_attributes is not None:
        real_attributes = paths._get_file_attributes

        def attributes(path):
            checked.append(canonical(path))
            return real_attributes(path)

        monkeypatch.setattr(paths, "_get_file_attributes", attributes)
    for _ in range(2):
        with ScanSession() as session:
            session.observe(tmp_path)
    assert checked.count(str(tmp_path)) == 4  # checked chain leaf + after, in each epoch
    assert len(enumerated) == 2
    for position in enumerated:
        assert checked[position - 2:position + 1] == [str(tmp_path.parent), str(tmp_path), str(tmp_path)]


def test_metadata_diagnostics_count_each_query_once_and_hide_paths(tmp_path, monkeypatch):
    (tmp_path / "private-name.md").write_bytes(b"private body")
    inaccessible = tmp_path / "private-missing"
    original = os.lstat

    def lstat(path, *args, **kwargs):
        if path == native(inaccessible):
            raise PermissionError(str(inaccessible))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    metrics = ScanMetrics()
    assert snapshot(tmp_path)["kind"] == "dir"
    assert not metrics.counts
    with metrics.activate(), phase("final_revalidation", tallies=("snapshot_calls", "directory_enumerations")):
        with ScanSession() as session:
            session.observe(tmp_path, full=False)
            session.observe(tmp_path)
        with pytest.raises(PermissionError):
            snapshot(inaccessible)
    counts = metrics.counts
    assert counts["lstat_errors"] == counts["direntry_stat_calls"] == counts["directory_enumerations"] == 1
    assert counts["metadata_upgrades"] == 1
    assert counts["snapshot_calls"] == counts["lstat_calls"] == counts["final_revalidation_snapshot_calls"]
    assert counts["metadata_queries"] == sum(counts[name] for name in
                                            ("lstat_calls", "direntry_stat_calls", "file_attributes_calls"))
    assert counts["final_revalidation_directory_enumerations"] == 1
    assert all(metrics.seconds[key] > 0 for key in ("lstat", "plain_chain", "direntry_stat", "final_revalidation"))
    assert "private" not in str(metrics.report())


def test_windows_discovery_metadata_is_upgraded_before_a_manifest(tmp_path):
    (tmp_path / "x").write_bytes(b"content")
    with ScanSession() as session:
        cheap = session.observe(tmp_path, full=False)
        full = session.observe(tmp_path)
        assert not cheap.full_metadata and full.full_metadata
        assert full.entries[0].state["inode"] == (tmp_path / "x").stat().st_ino
        assert full.entries[0].state["device"] == (tmp_path / "x").stat().st_dev


@pytest.mark.parametrize("cancelled", [False, True])
def test_enumeration_handle_closes_when_iteration_fails_or_is_cancelled(tmp_path, monkeypatch, cancelled):
    import obmanage.scanning.session as implementation
    cancel = Event()
    closed = []
    class Directory:
        def __enter__(self): return self
        def __exit__(self, *_): closed.append(True)
        def __iter__(self): return self
        def __next__(self):
            if cancelled:
                cancel.set()
                raise SyncCancelled("injected")
            raise OSError("injected")
    monkeypatch.setattr(implementation, "scandir", lambda _: Directory())
    with ScanSession(cancel) as session:
        if cancelled:
            with pytest.raises(SyncCancelled): session.manifest(tmp_path)
        else:
            assert not session.manifest(tmp_path).complete
    assert closed == [True]


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError, OSError])
def test_failed_leaf_snapshot_keeps_partial_results_but_blocks_manifest_consumption(tmp_path, monkeypatch, failure):
    import obmanage.scanning.session as implementation
    for name in ("good.md", "bad.md"):
        (tmp_path / name).write_bytes(b"data")
    original = implementation.snapshot
    def snapshot(path):
        if str(path).endswith("bad.md"): raise failure("injected")
        return original(path)
    monkeypatch.setattr(implementation, "snapshot", snapshot)
    with ScanSession() as session:
        result = session.manifest(tmp_path)
        assert "good.md" in result.entries and not result.complete
        with pytest.raises(SyncError): result.require_complete()
