import os
from threading import Event, Lock, Thread, enumerate as live_threads

import pytest

from tools.benchmark_scan_experiments import _statistics, pool_variant
from tools.benchmark_scanning import BASELINE, reference_package, signature


@pytest.fixture(scope="module")
def reference():
    with reference_package(BASELINE) as modules:
        yield modules


def make_notes(vault, count):
    (vault / ".obsidian").mkdir(parents=True)
    for index in range(count):
        (vault / f"note-{index:02d}.md").write_bytes("a中🙂\n".encode() * 20)


def test_pool_variants_match_all_fields_and_keep_issues_in_order(tmp_path, reference, monkeypatch):
    root = tmp_path / "collection"
    first = root / "first"
    make_notes(first, 27)
    make_notes(first / "nested", 10)
    make_notes(root / "last", 20)
    (first / "note-00.md").write_bytes(b"\xff")
    (first / "note-01.md").write_bytes(b"a" * ((1024 * 1024) - 1) + "中🙂".encode())
    (first / ".trash").mkdir()
    (first / ".trash" / "discard.md").write_bytes(b"discard")
    module = reference["management.statistics"]
    read = module._read_utf8_characters

    def fault(path, *args, **kwargs):
        if path.endswith("note-03.md"):
            raise PermissionError("synthetic unreadable note")
        if path.endswith("note-04.md"):
            raise OSError("synthetic replacement during read")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(module, "_read_utf8_characters", fault)
    expected = signature(_statistics(reference, root, include_trash=True))
    assert any(issue["code"] == "markdown_invalid_utf8" for issue in expected["issues"])
    assert any(issue["code"] == "markdown_unreadable" for issue in expected["issues"])
    for workers in (1, 2, 4):
        for _ in range(2):
            progress = []
            with pool_variant(module, workers):
                actual = _statistics(reference, root, include_trash=True, progress=progress.append)
            assert signature(actual) == expected
            files = [entry.completed_files for entry in progress if entry.phase == "statistics"]
            assert files == sorted(files) and files[-1] == 58
            assert not any(t.name.startswith("obmanage-statistics") for t in live_threads())


@pytest.mark.parametrize("workers", (None, 1, 2, 4))
def test_pool_cancellation_releases_threads_and_file_handles(reference, tmp_path, monkeypatch, workers):
    root = tmp_path / "collection"
    make_notes(root / "vault", 32)
    cancel, started = Event(), Event()
    module = reference["management.statistics"]
    real_open, real_close, real_read = os.open, os.close, os.read
    descriptors = set()
    lock = Lock()

    def track_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if str(path).endswith(".md"):
            with lock:
                descriptors.add(handle)
        return handle

    def track_close(handle):
        with lock:
            descriptors.discard(handle)
        return real_close(handle)

    def track_read(handle, amount):
        with lock:
            tracked = handle in descriptors
        if tracked:
            started.set()
            assert cancel.wait(10)
        return real_read(handle, amount)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", track_open)
        scoped.setattr(os, "close", track_close)
        scoped.setattr(os, "read", track_read)
        requester = Thread(target=lambda: (started.wait(10), cancel.set()))
        requester.start()
        try:
            with pytest.raises(module.SyncCancelled), pool_variant(module, workers):
                _statistics(reference, root, cancel=cancel)
        finally:
            requester.join(10)
        assert started.is_set() and not requester.is_alive()
        assert not descriptors
        assert not any(t.name.startswith("obmanage-statistics") for t in live_threads())
    assert not _statistics(reference, root).issues


def test_optional_diagnostics_and_cancel_before_open(tmp_path, monkeypatch):
    from obmanage.management import statistics
    from obmanage.scanning.metrics import ScanMetrics

    root = tmp_path / "collection"
    for i in range(7):
        make_notes(root / f"vault-{i}", i + 1)
    metrics = ScanMetrics("statistics")
    with metrics.activate():
        result = statistics.collect_vault_statistics(tuple(root.iterdir()))
    assert not result.issues
    report = metrics.report()
    assert report["counts"]["statistics_pools"] == 7
    assert 7 <= report["counts"]["statistics_workers_started"] <= 22
    assert len(report["slow_vaults"]) == 5
    assert str(tmp_path) not in repr(report) and "note-" not in repr(report)
    assert report["seconds"]["statistics_utf8"] <= report["seconds"]["total"]

    metrics = ScanMetrics("metadata_only")
    with metrics.activate():
        statistics.collect_vault_statistics((), count_characters=True)
        statistics.collect_vault_statistics(tuple(root.iterdir()), count_characters=False)
        empty = tmp_path / "no-markdown"
        make_notes(empty, 0)
        statistics.collect_vault_statistics(empty)
    assert metrics.counts["statistics_pools"] == metrics.counts["markdown_bytes"] == 0

    cancel = Event()
    cancel.set()
    monkeypatch.setattr(os, "open", lambda *_: pytest.fail("queued cancelled note opened"))
    with pytest.raises(statistics.SyncCancelled):
        statistics._read_utf8_characters("unused", {}, cancel=cancel, on_bytes=lambda _: None)
