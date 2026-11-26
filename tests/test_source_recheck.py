"""Exercise native advisory breaks through the reviewed mirror workflow."""
import os
from dataclasses import replace
from threading import Event

import pytest

from obmanage.engine import SyncEngine
from obmanage.paths import canonical
from obmanage.scanning.metrics import ScanMetrics
from obmanage.source_recheck import SourceRecheck, _WindowsReadCache
from obmanage.sync_workflow import SyncIntent, SyncRequest, run_sync


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows read-cache oplocks")
    source, target = tmp_path / "source", tmp_path / "target"
    for endpoint in (source, target):
        for subdir in ("editing", "quiet"):
            folder = endpoint / subdir
            folder.mkdir(parents=True)
            (folder / "keep.md").write_bytes(b"same")
    (source / "new.md").write_bytes(b"new")
    (target / "old.md").write_bytes(b"obsolete")
    engine = SyncEngine(tmp_path / "state")
    original = SourceRecheck.for_plan
    pools = []

    def enable_native_for_fixture(plan):
        # Only the optimization's filesystem selector is overridden. The engine
        # verifies the real root, volume and snapshots throughout execution.
        local_exfat = r"\\?\Volume{fixture}\:00000001:exFAT"
        cache = original(replace(plan, context={**plan.context, "source_volume": local_exfat}))
        pools.append(cache.cache)
        return cache

    monkeypatch.setattr(SourceRecheck, "for_plan", staticmethod(enable_native_for_fixture))
    return engine, source, target, pools


def preview(engine, source, target, mode="mirror"):
    request = SyncRequest(str(source), str(target), "移动硬盘", "本机", mode, SyncIntent.PREVIEW)
    plan = run_sync(engine, request, Event(), None)
    assert plan.can_execute
    return replace(request, intent=SyncIntent.MANUAL, reviewed_plan=plan)


@pytest.mark.parametrize("mode", ["mirror", "no_video"])
def test_click_walks_source_once_and_reuses_quiet_files(reviewed, mode):
    engine, source, target, pools = reviewed
    if mode == "no_video":
        (source / "keep.mp4").write_bytes(b"source video")
        (target / "keep.mp4").write_bytes(b"target video")
    request = preview(engine, source, target, mode)
    assert not pools
    phases = frozenset(("preflight_source", "barrier_source", "post_delete_source", "post_delete_target"))
    metrics = ScanMetrics(detail_phases=phases)
    with metrics.activate():
        result = run_sync(engine, request, Event(), None)
    assert result.status == "success", result.errors
    scopes = metrics.report()["scopes"]
    assert scopes["preflight_source"]["directory_enumerations"] == 3
    for phase in ("barrier_source", "post_delete_source"):
        assert scopes[phase].get("directory_enumerations", 0) == 0
        assert scopes[phase]["source_reused_skipped_files"] == 2
        assert scopes[phase]["source_rechecked_paths"] == 1  # the new file and its live path chain
    assert scopes["post_delete_target"]["directory_enumerations"] == 3
    assert len(pools) == 1 and not pools[0].pending
    if mode == "no_video":
        assert (target / "keep.mp4").read_bytes() == b"target video"


@pytest.mark.parametrize("stage", ["copy", "delete"])
@pytest.mark.parametrize("mutation", [
    "open_append", "add", "case", "mtime", "birth", "replace", "directory", "root", "directory_mtime",
])
def test_live_change_in_previously_skipped_subtree_is_detected(reviewed, stage, mutation):
    engine, source, target, pools = reviewed
    request = preview(engine, source, target)
    folder = source / "editing"
    keep = folder / "keep.md"
    changed = False
    writer = None

    def edit(event):
        nonlocal changed, writer
        if changed or event.phase != stage:
            return
        changed = True
        if mutation == "open_append":
            writer = keep.open("ab", buffering=0)
            writer.write(b"still open during the next check")
        elif mutation == "add":
            (folder / "arrived.md").write_bytes(b"new")
        elif mutation == "case":
            # exFAT may accept a case-only rename without changing its spelling.
            keep.rename(folder / "hop.md").rename(folder / "KEEP.md")
            assert "KEEP.md" in os.listdir(folder)
        elif mutation == "mtime":
            st = keep.stat()
            os.utime(keep, ns=(st.st_atime_ns, st.st_mtime_ns - 5_000_000_000))
        elif mutation == "birth":
            import ctypes
            import msvcrt
            from ctypes import wintypes as w

            previous = keep.stat().st_birthtime_ns
            ticks = (previous - 10_000_000_000) // 100 + 116444736000000000
            stamp = w.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
            setter = ctypes.WinDLL("kernel32", use_last_error=True).SetFileTime
            setter.argtypes = (w.HANDLE, ctypes.POINTER(w.FILETIME), w.LPVOID, w.LPVOID)
            with keep.open("r+b") as stream:
                assert setter(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(stamp), None, None)
            assert keep.stat().st_birthtime_ns != previous
        elif mutation == "replace":
            replacement = folder / "replacement"
            replacement.write_bytes(b"same")
            os.replace(replacement, keep)
        elif mutation == "root":
            source.rename(source.with_name("moved-source"))
            source.mkdir()
        elif mutation == "directory_mtime":
            st = folder.stat()
            os.utime(folder, ns=(st.st_atime_ns, st.st_mtime_ns - 5_000_000_000))
        else:
            folder.rename(source / "moved")
            folder.mkdir()
            (folder / "keep.md").write_bytes(b"different")

    try:
        result = run_sync(engine, request, Event(), edit)
    finally:
        if writer is not None:
            writer.close()
    assert changed and result.status == "failed" and not result.preflight_failed, result
    assert result.copied_files == int((mutation, stage) != ("root", "copy"))
    if mutation == "root":
        assert source.with_name("moved-source").is_dir()
    assert result.deleted_files == int(stage == "delete")
    assert (target / "old.md").exists() == (stage == "copy")
    assert (target / "editing" / "keep.md").read_bytes() == b"same"
    assert pools and not pools[0].pending


@pytest.mark.parametrize("missing", ["root", "directory", "file"])
def test_denied_read_cache_grant_falls_back_to_live_scope(reviewed, monkeypatch, missing):
    engine, source, target, pools = reviewed
    request = preview(engine, source, target)
    folder = source / "editing"
    keep = folder / "keep.md"
    denied = {"root": source, "directory": folder, "file": keep}[missing]
    watch = _WindowsReadCache.watch

    def grant(cache, path):
        if os.path.normcase(canonical(path)) == os.path.normcase(str(denied)):
            return None
        return watch(cache, path)

    monkeypatch.setattr(_WindowsReadCache, "watch", grant)
    with keep.open("ab", buffering=0) as writer:
        edited = False
        def edit(event):
            nonlocal edited
            if event.phase == "copy" and not edited:
                edited = True
                writer.write(b"open writer")
        metrics = ScanMetrics(detail_phases=frozenset(("barrier_source",)))
        with metrics.activate():
            result = run_sync(engine, request, Event(), edit)
    assert result.status == "failed" and result.copied_files == 1 and not result.deleted_files
    assert (target / "old.md").is_file() and not pools[0].pending
    scope = metrics.report()["scopes"]["barrier_source"]
    assert scope.get("directory_enumerations", 0) == {"root": 3, "directory": 1, "file": 0}[missing]


def test_preflight_change_and_cancellation_release_every_guard(reviewed):
    engine, source, target, pools = reviewed
    request = preview(engine, source, target)
    path = source / "editing" / "keep.md"
    path.write_bytes(b"edited after preview")
    result = run_sync(engine, request, Event(), None)
    assert result.preflight_failed and not result.copied_files and not result.deleted_files
    assert not (target / "new.md").exists() and (target / "old.md").exists()
    assert not pools[0].pending

    path.write_bytes(b"same")
    request = preview(engine, source, target)
    stop = Event()
    result = run_sync(engine, request, stop, lambda event: stop.set() if event.phase == "copy" else None)
    assert result.status == "cancelled" and not result.deleted_files
    assert not pools[-1].pending


def test_handle_limit_checks_uncovered_entries_and_never_holds_the_drive_at_preview(reviewed, monkeypatch):
    import obmanage.source_recheck as tracking

    engine, source, target, pools = reviewed
    monkeypatch.setattr(tracking, "_HANDLE_LIMIT", 1)
    request = preview(engine, source, target)
    assert not pools
    stop_after_copy = False
    def mutate(event):
        nonlocal stop_after_copy
        if event.phase == "copy" and not stop_after_copy:
            stop_after_copy = True
            (source / "quiet" / "keep.md").write_bytes(b"changed outside the handle limit")
    metrics = ScanMetrics()
    with metrics.activate():
        result = run_sync(engine, request, Event(), mutate)
    assert result.status == "failed" and result.deleted_files == 0
    assert (target / "old.md").exists()
    assert metrics.counts["source_read_cache_handles"] == 1
