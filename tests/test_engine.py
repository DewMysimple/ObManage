from __future__ import annotations

import os
import errno
import stat
from pathlib import Path
from threading import Barrier, Event, Lock, enumerate as enumerate_threads, get_ident
from types import SimpleNamespace

import pytest

from obmanage.engine import SyncEngine
from obmanage.models import SyncCancelled, SyncError
from obmanage.store import BaselineStore
import obmanage.engine as engine_module
import obmanage.paths as paths_module


@pytest.fixture
def mirror(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    return SyncEngine(tmp_path / "state"), source, target


def put(root: Path, relative: str, content: bytes = b"content") -> Path:
    file = root / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)
    return file


def analyze(engine, source, target, **kwargs):
    return engine.analyze(str(source), str(target), **kwargs)


def synchronize(engine, source, target, **kwargs):
    plan = analyze(engine, source, target)
    assert plan.can_execute, plan.errors
    result = engine.execute(plan, **kwargs)
    assert not result.errors, result.errors
    return plan, result


def require_rejected(operation):
    """Safety failures may surface as an exception or structured error result."""
    try:
        outcome = operation()
    except (SyncError, SyncCancelled):
        return
    assert outcome.errors or getattr(outcome, "status", "") in {
        "cancelled", "canceled", "error", "failed", "blocked", "partial"
    }, outcome


def pretend_separate_volumes(monkeypatch, source: Path, target: Path) -> None:
    real_volume_identity = paths_module.volume_identity

    def separate_test_volumes(path):
        value = paths_module.canonical(path)
        if os.path.commonpath((value, str(source))) == str(source):
            return "test-source-volume"
        if os.path.commonpath((value, str(target))) == str(target):
            return "test-target-volume"
        return real_volume_identity(path)

    monkeypatch.setattr(paths_module, "volume_identity", separate_test_volumes)


def test_engine_instances_share_one_process_task_mutex(tmp_path):
    first = SyncEngine(tmp_path / "first-state")
    second = SyncEngine(tmp_path / "second-state")
    assert first._lock.acquire(blocking=False)
    try:
        with pytest.raises(SyncError, match="已有同步任务"):
            second.analyze(str(tmp_path / "source"), str(tmp_path / "target"))
    finally:
        first._lock.release()


def simulate_reissued_staging_identity(monkeypatch):
    """Model a filesystem that assigns a new file ID when staging is renamed."""
    real_utime = engine_module.os.utime
    real_snapshot = engine_module.snapshot
    staged_paths = set()

    def remember_staged_path(path, *args, **kwargs):
        result = real_utime(path, *args, **kwargs)
        value = paths_module.canonical(path)
        if os.path.basename(value).startswith(".obmanage-") and value.endswith(".tmp"):
            staged_paths.add(os.path.normcase(value))
        return result

    def snapshot_with_pre_rename_identity(path):
        state = real_snapshot(path)
        if state is not None and os.path.normcase(paths_module.canonical(path)) in staged_paths:
            state = dict(state)
            state["inode"] += 1
        return state

    monkeypatch.setattr(engine_module.os, "utime", remember_staged_path)
    monkeypatch.setattr(engine_module, "snapshot", snapshot_with_pre_rename_identity)


def test_first_mirror_preserves_hidden_files_and_empty_directories(mirror):
    engine, source, target = mirror
    put(source, "笔记/中文.md", "第一篇笔记".encode())
    put(source, ".obsidian/app.json", b'{"theme":"dark"}')
    (source / "空目录" / "仍然为空").mkdir(parents=True)

    plan, result = synchronize(engine, source, target)

    assert plan.counts["add"] == 2
    assert result.copied_files == 2
    assert (target / "笔记/中文.md").read_bytes() == (source / "笔记/中文.md").read_bytes()
    assert (target / ".obsidian/app.json").read_bytes() == b'{"theme":"dark"}'
    assert (target / "空目录/仍然为空").is_dir()


def test_copy_progress_is_monotonic_across_write_and_verification(mirror):
    engine, source, target = mirror
    first = put(source, "first.bin", b"a" * 17)
    second = put(source, "second.bin", b"b" * 32)
    plan = analyze(engine, source, target)
    events = []

    result = engine.execute(plan, progress=events.append)

    assert result.status == "success", result.errors
    transfer = [
        event for event in events
        if event.phase in {"copy", "copied"} and event.total_bytes
    ]
    assert transfer
    values = [event.completed_bytes for event in transfer]
    assert values == sorted(values)
    assert values[-1] == first.stat().st_size + second.stat().st_size
    assert {event.total_bytes for event in transfer} == {values[-1]}
    assert {event.message for event in transfer} == {"正在复制并校验"}


def test_next_source_write_overlaps_current_target_verification(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "first.bin", b"a" * 4096)
    put(source, "second.bin", b"b" * 4096)
    pretend_separate_volumes(monkeypatch, source, target)
    plan = analyze(engine, source, target)
    real_copy = engine_module.copy_stream_and_hash
    real_hash = engine_module._hash_file
    second_write_started = Event()
    copy_calls = 0
    copy_lock = Lock()

    def observed_copy(*args, **kwargs):
        nonlocal copy_calls
        with copy_lock:
            copy_calls += 1
            if copy_calls == 2:
                second_write_started.set()
        return real_copy(*args, **kwargs)

    first_verification = True

    def require_pipeline(path, *args, **kwargs):
        nonlocal first_verification
        if first_verification and Path(path).name.startswith(".obmanage-"):
            first_verification = False
            assert second_write_started.wait(timeout=3), (
                "The next registered temp must start writing before the current temp is read back"
            )
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "copy_stream_and_hash", observed_copy)
    monkeypatch.setattr(engine_module, "_hash_file", require_pipeline)
    caller_thread = get_ident()
    callback_threads = []

    result = engine.execute(
        plan, progress=lambda event: callback_threads.append(get_ident())
    )

    assert result.status == "success", result.errors
    assert copy_calls == 2
    assert callback_threads and set(callback_threads) == {caller_thread}
    assert not any(thread.name.startswith("obmanage-copy") for thread in enumerate_threads())


def test_same_volume_keeps_copy_and_writeback_verification_serial(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "first.bin", b"a" * 4096)
    put(source, "second.bin", b"b" * 4096)
    plan = analyze(engine, source, target)
    assert plan.context["source_volume"] == plan.context["target_volume"]
    real_copy = engine_module.copy_stream_and_hash
    real_hash = engine_module._hash_file
    second_write_started = Event()
    copy_calls = 0

    def observed_copy(*args, **kwargs):
        nonlocal copy_calls
        copy_calls += 1
        if copy_calls == 2:
            second_write_started.set()
        return real_copy(*args, **kwargs)

    checked_first = False

    def require_serial(path, *args, **kwargs):
        nonlocal checked_first
        if not checked_first and Path(path).name.startswith(".obmanage-"):
            checked_first = True
            assert not second_write_started.is_set(), (
                "Same-volume media must not interleave the next write with this read"
            )
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "copy_stream_and_hash", observed_copy)
    monkeypatch.setattr(engine_module, "_hash_file", require_serial)

    result = engine.execute(plan)

    assert result.status == "success", result.errors
    assert checked_first and copy_calls == 2


@pytest.mark.parametrize(("with_deletion", "expected_scans_per_root"), [
    (False, 2),
    (True, 3),
])
def test_final_full_scan_is_repeated_only_after_actual_deletions(
        mirror, monkeypatch, with_deletion, expected_scans_per_root):
    engine, source, target = mirror
    put(source, "new.md", b"new content")
    if with_deletion:
        put(target, "obsolete.md", b"remove only after copy verification")
    plan = analyze(engine, source, target)
    real_scan = engine_module._scan
    scan_counts = {str(source): 0, str(target): 0}

    def counted_scan(root, *args, **kwargs):
        scan_counts[paths_module.canonical(root)] += 1
        return real_scan(root, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_scan", counted_scan)

    result = engine.execute(plan)

    assert result.status == "success", result.errors
    assert scan_counts == {
        str(source): expected_scans_per_root,
        str(target): expected_scans_per_root,
    }


def test_pipeline_verification_failure_commits_nothing_and_cleans_next_temp(
        mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "first.bin", b"a" * 4096)
    put(source, "second.bin", b"b" * 4096)
    put(target, "obsolete.md", b"must survive")
    pretend_separate_volumes(monkeypatch, source, target)
    plan = analyze(engine, source, target)
    real_copy = engine_module.copy_stream_and_hash
    real_hash = engine_module._hash_file
    second_write_started = Event()
    copy_calls = 0
    copy_lock = Lock()

    def observed_copy(*args, **kwargs):
        nonlocal copy_calls
        with copy_lock:
            copy_calls += 1
            if copy_calls == 2:
                second_write_started.set()
        return real_copy(*args, **kwargs)

    corrupted = False

    def fail_first_verification(path, *args, **kwargs):
        nonlocal corrupted
        digest = real_hash(path, *args, **kwargs)
        if not corrupted and Path(path).name.startswith(".obmanage-"):
            assert second_write_started.wait(timeout=3)
            corrupted = True
            return "0" * 64
        return digest

    monkeypatch.setattr(engine_module, "copy_stream_and_hash", observed_copy)
    monkeypatch.setattr(engine_module, "_hash_file", fail_first_verification)

    result = engine.execute(plan)

    assert result.status == "failed"
    assert result.copied_files == 0
    assert (target / "obsolete.md").read_bytes() == b"must survive"
    assert not (target / "first.bin").exists()
    assert not (target / "second.bin").exists()
    assert not any(path.name.startswith(".obmanage-") for path in target.iterdir())
    store = BaselineStore(engine.state_dir)
    try:
        assert store.temps(plan.pair_id) == []
        assert store.records(plan.pair_id) == {}
    finally:
        store.close()
    assert not any(thread.name.startswith("obmanage-copy") for thread in enumerate_threads())


def test_pipeline_failure_cancels_and_joins_running_next_writer(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "first.bin", b"a" * 4096)
    put(source, "second.bin", b"b" * 4096)
    pretend_separate_volumes(monkeypatch, source, target)
    plan = analyze(engine, source, target)
    real_copy = engine_module.copy_stream_and_hash
    real_hash = engine_module._hash_file
    second_write_started = Event()
    second_write_finished = Event()
    pause = Event()
    copy_calls = 0
    copy_lock = Lock()

    def block_second_copy(*args, **kwargs):
        nonlocal copy_calls
        with copy_lock:
            copy_calls += 1
            this_call = copy_calls
        if this_call == 1:
            return real_copy(*args, **kwargs)
        second_write_started.set()
        try:
            while True:
                kwargs["check_cancel"]()
                pause.wait(0.001)
        finally:
            second_write_finished.set()

    failed_first = False

    def fail_first_verification(path, *args, **kwargs):
        nonlocal failed_first
        digest = real_hash(path, *args, **kwargs)
        if not failed_first and Path(path).name.startswith(".obmanage-"):
            assert second_write_started.wait(timeout=3)
            failed_first = True
            return "0" * 64
        return digest

    monkeypatch.setattr(engine_module, "copy_stream_and_hash", block_second_copy)
    monkeypatch.setattr(engine_module, "_hash_file", fail_first_verification)

    result = engine.execute(plan)

    assert result.status == "failed"
    assert second_write_finished.is_set()
    assert not (target / "first.bin").exists()
    assert not (target / "second.bin").exists()
    assert not any(path.name.startswith(".obmanage-") for path in target.iterdir())
    assert not any(thread.name.startswith("obmanage-copy") for thread in enumerate_threads())


def test_full_root_chain_checks_are_batched_during_small_file_copy(mirror, monkeypatch):
    engine, source, target = mirror
    for number in range(65):
        put(source, f"small-{number:03d}.bin", b"data")
    plan = analyze(engine, source, target)
    real_revalidate = engine_module.revalidate_roots
    calls = []
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: 100.0)

    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return real_revalidate(*args, **kwargs)

    monkeypatch.setattr(engine_module, "revalidate_roots", counted)
    result = engine.execute(plan)

    assert result.status == "success", result.errors
    # The complete selected-volume/root check happens at phase barriers and at
    # least every 16 files or 250 ms, rather than twice for every tiny file.
    assert 4 <= len(calls) < 20


def test_mirror_never_copies_or_deletes_deployment_recovery_directories(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"active source")
    source_recovery = source / ".obmanage-deploy-source.backup"
    target_recovery = target / ".obmanage-deploy-target.rollback"
    put(source_recovery, "private.md", b"must not be mirrored")
    put(target_recovery, "original.md", b"only recoverable original")

    plan, result = synchronize(engine, source, target)

    assert result.status == "success"
    assert (target / "note.md").read_bytes() == b"active source"
    assert not (target / source_recovery.name).exists()
    assert (target_recovery / "original.md").read_bytes() == b"only recoverable original"
    assert all(".obmanage-deploy-" not in item.relative_path for item in plan.items)


def test_incremental_updates_deletes_and_target_only_files(mirror):
    engine, source, target = mirror
    put(source, "unchanged/video.bin", b"unchanged video" * 100)
    put(source, "updated.md", b"original")
    put(source, "removed/deep/note.md", b"delete me")
    synchronize(engine, source, target)
    unchanged = target / "unchanged/video.bin"
    unchanged_metadata = unchanged.stat()
    put(source, "updated.md", b"updated source")
    put(source, "new.md", b"new")
    (source / "removed/deep/note.md").unlink()
    (source / "removed/deep").rmdir()
    (source / "removed").rmdir()
    put(target, "target-only/note.md", b"local target addition")

    plan, result = synchronize(engine, source, target)

    assert plan.counts["add"] == 1
    assert plan.counts["update"] == 1
    assert result.copied_files == 2
    assert result.copied_bytes == len(b"updated source") + len(b"new")
    assert result.deleted_files == 2
    assert (target / "updated.md").read_bytes() == b"updated source"
    assert not (target / "removed").exists()
    assert not (target / "target-only").exists()
    assert unchanged.stat().st_mtime_ns == unchanged_metadata.st_mtime_ns
    assert unchanged.stat().st_ino == unchanged_metadata.st_ino


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_readonly_target_only_files_are_deleted_and_cleanup_continues(mirror):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    first = put(target, "old/deep/first.jpg", b"first target-only file")
    second = put(target, "old/deep/second.jpg", b"second target-only file")
    first.chmod(stat.S_IREAD)
    second.chmod(stat.S_IREAD)
    try:
        plan, result = synchronize(engine, source, target)

        assert plan.counts["delete"] == 2
        assert plan.counts["rmdir"] == 2
        assert result.deleted_files == 2
        assert result.deleted_dirs == 2
        assert not (target / "old").exists()
    finally:
        for path in (first, second):
            if path.exists():
                path.chmod(stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_readonly_existing_target_is_replaced_before_deletions(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"authoritative source content")
    destination = put(target, "note.md", b"old target")
    obsolete = put(target, "obsolete.md", b"delete only after replacement")
    destination.chmod(stat.S_IREAD)
    try:
        plan, result = synchronize(engine, source, target)

        assert plan.counts["update"] == 1
        assert result.copied_files == 1
        assert destination.read_bytes() == b"authoritative source content"
        assert destination.stat().st_mode & stat.S_IWRITE
        assert not obsolete.exists()
    finally:
        if destination.exists():
            destination.chmod(stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_non_readonly_access_denied_is_not_retried_with_chmod(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    blocked = put(target, "a-blocked.md", b"ordinary writable target")
    untouched = put(target, "z-untouched.md", b"later deletion must not run")
    plan = analyze(engine, source, target)
    real_unlink = engine_module.os.unlink
    chmod_calls = []

    def denied_unlink(path):
        if paths_module.canonical(path) == paths_module.canonical(blocked):
            raise PermissionError(errno.EACCES, "simulated access denied", path, 5)
        return real_unlink(path)

    def record_chmod(*args, **kwargs):
        chmod_calls.append((args, kwargs))

    monkeypatch.setattr(engine_module.os, "unlink", denied_unlink)
    monkeypatch.setattr(engine_module.os, "chmod", record_chmod)

    result = engine.execute(plan)

    assert result.status == "failed"
    assert blocked.exists()
    assert untouched.exists()
    assert chmod_calls == []


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_failed_readonly_delete_restores_attribute_and_stops_cleanup(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    blocked = put(target, "a-blocked.md", b"read-only target")
    untouched = put(target, "z-untouched.md", b"later deletion must not run")
    blocked.chmod(stat.S_IREAD)
    plan = analyze(engine, source, target)
    real_unlink = engine_module.os.unlink

    def denied_unlink(path):
        if paths_module.canonical(path) == paths_module.canonical(blocked):
            raise PermissionError(errno.EACCES, "simulated persistent denial", path, 5)
        return real_unlink(path)

    monkeypatch.setattr(engine_module.os, "unlink", denied_unlink)
    try:
        result = engine.execute(plan)

        assert result.status == "failed"
        assert blocked.exists()
        assert not blocked.stat().st_mode & stat.S_IWRITE
        assert untouched.exists()
    finally:
        if blocked.exists():
            blocked.chmod(stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="Windows hard-link attributes are shared")
def test_retry_failure_does_not_restore_readonly_after_new_hardlink(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    blocked = put(target, "a-blocked.md", b"read-only target")
    untouched = put(target, "z-untouched.md", b"later deletion must not run")
    outside_alias = target.parent / "outside-alias.md"
    blocked.chmod(stat.S_IREAD)
    plan = analyze(engine, source, target)
    real_chmod = engine_module.os.chmod
    real_unlink = engine_module.os.unlink
    attempts = []
    chmod_calls = []

    def deny_and_add_alias(path):
        if paths_module.canonical(path) == paths_module.canonical(blocked):
            attempts.append(path)
            if len(attempts) == 2:
                os.link(path, outside_alias)
            raise PermissionError(errno.EACCES, "simulated persistent denial", path, 5)
        return real_unlink(path)

    def record_chmod(path, mode):
        chmod_calls.append((paths_module.canonical(path), mode))
        return real_chmod(path, mode)

    monkeypatch.setattr(engine_module.os, "unlink", deny_and_add_alias)
    monkeypatch.setattr(engine_module.os, "chmod", record_chmod)

    result = engine.execute(plan)

    assert result.status == "failed"
    assert len(attempts) == 2
    assert len(chmod_calls) == 1
    assert chmod_calls[0][1] & stat.S_IWRITE
    assert blocked.stat().st_mode & stat.S_IWRITE
    assert outside_alias.stat().st_mode & stat.S_IWRITE
    assert untouched.exists()


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
@pytest.mark.parametrize("winerror", [5, 183])
def test_failed_readonly_replace_restores_old_target_and_prevents_deletion(
        mirror, monkeypatch, winerror):
    engine, source, target = mirror
    put(source, "note.md", b"authoritative source content")
    destination = put(target, "note.md", b"old target")
    obsolete = put(target, "obsolete.md", b"must survive failed replacement")
    destination.chmod(stat.S_IREAD)
    plan = analyze(engine, source, target)

    def denied_replace(staged, target_path):
        error = errno.EEXIST if winerror == 183 else errno.EACCES
        raise OSError(error, "simulated persistent denial", target_path, winerror)

    monkeypatch.setattr(engine_module.os, "replace", denied_replace)
    try:
        result = engine.execute(plan)

        assert result.status == "failed"
        assert destination.read_bytes() == b"old target"
        assert not destination.stat().st_mode & stat.S_IWRITE
        assert obsolete.exists()
    finally:
        if destination.exists():
            destination.chmod(stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_post_chmod_target_change_stops_before_retry_and_restores_readonly(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    blocked = put(target, "a-blocked.md", b"previewed target")
    untouched = put(target, "z-untouched.md", b"later deletion must not run")
    blocked.chmod(stat.S_IREAD)
    plan = analyze(engine, source, target)
    real_chmod = engine_module.os.chmod
    real_unlink = engine_module.os.unlink
    unlink_attempts = []

    def denied_unlink(path):
        if paths_module.canonical(path) == paths_module.canonical(blocked):
            unlink_attempts.append(path)
            raise PermissionError(errno.EACCES, "simulated access denied", path, 5)
        return real_unlink(path)

    def chmod_then_change(path, mode):
        result = real_chmod(path, mode)
        if (paths_module.canonical(path) == paths_module.canonical(blocked)
                and mode & stat.S_IWRITE):
            with open(path, "ab") as stream:
                stream.write(b" externally changed")
        return result

    monkeypatch.setattr(engine_module.os, "unlink", denied_unlink)
    monkeypatch.setattr(engine_module.os, "chmod", chmod_then_change)
    try:
        result = engine.execute(plan)

        assert result.status == "failed"
        assert len(unlink_attempts) == 1
        assert blocked.read_bytes().endswith(b" externally changed")
        assert not blocked.stat().st_mode & stat.S_IWRITE
        assert untouched.exists()
    finally:
        if blocked.exists():
            real_chmod(blocked, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_readonly_source_is_never_chmodded_when_readonly_target_is_replaced(mirror, monkeypatch):
    engine, source, target = mirror
    origin = put(source, "note.md", b"authoritative source content")
    destination = put(target, "note.md", b"old target")
    origin.chmod(stat.S_IREAD)
    destination.chmod(stat.S_IREAD)
    origin_before = paths_module.snapshot(origin)
    origin_attributes = origin.stat().st_file_attributes
    real_chmod = engine_module.os.chmod

    def forbid_source_chmod(path, mode):
        assert paths_module.canonical(path) != paths_module.canonical(origin)
        return real_chmod(path, mode)

    monkeypatch.setattr(engine_module.os, "chmod", forbid_source_chmod)
    try:
        _, result = synchronize(engine, source, target)

        assert result.copied_files == 1
        assert origin.read_bytes() == b"authoritative source content"
        assert paths_module.snapshot(origin) == origin_before
        assert origin.stat().st_file_attributes == origin_attributes
    finally:
        real_chmod(origin, stat.S_IWRITE)
        if destination.exists():
            real_chmod(destination, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="Windows hard-link attributes are shared")
def test_readonly_target_hardlink_to_source_is_rejected_before_chmod(mirror, monkeypatch):
    engine, source, target = mirror
    origin = put(source, "source-note.md", b"source hard-link content")
    target_link = target / "target-only.md"
    os.link(origin, target_link)
    target_link.chmod(stat.S_IREAD)
    origin_before = paths_module.snapshot(origin)
    origin_attributes = origin.stat().st_file_attributes
    plan = analyze(engine, source, target)
    real_chmod = engine_module.os.chmod
    chmod_calls = []

    def record_chmod(*args, **kwargs):
        chmod_calls.append((args, kwargs))
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(engine_module.os, "chmod", record_chmod)
    try:
        result = engine.execute(plan)

        assert result.status == "failed"
        assert target_link.exists()
        assert origin.read_bytes() == b"source hard-link content"
        assert paths_module.snapshot(origin) == origin_before
        assert origin.stat().st_file_attributes == origin_attributes
        assert chmod_calls == []
    finally:
        real_chmod(origin, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_replaced_target_after_denial_is_not_chmodded_or_deleted(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    blocked = put(target, "a-blocked.md", b"previewed target")
    untouched = put(target, "z-untouched.md", b"later deletion must not run")
    blocked.chmod(stat.S_IREAD)
    plan = analyze(engine, source, target)
    real_chmod = engine_module.os.chmod
    real_unlink = engine_module.os.unlink
    chmod_calls = []

    def replace_then_deny(path):
        if paths_module.canonical(path) == paths_module.canonical(blocked):
            real_chmod(path, stat.S_IWRITE)
            real_unlink(path)
            Path(paths_module.canonical(path)).write_bytes(b"external replacement")
            real_chmod(path, stat.S_IREAD)
            raise PermissionError(errno.EACCES, "simulated access denied", path, 5)
        return real_unlink(path)

    def record_chmod(*args, **kwargs):
        chmod_calls.append((args, kwargs))
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(engine_module.os, "unlink", replace_then_deny)
    monkeypatch.setattr(engine_module.os, "chmod", record_chmod)
    try:
        result = engine.execute(plan)

        assert result.status == "failed"
        assert blocked.read_bytes() == b"external replacement"
        assert not blocked.stat().st_mode & stat.S_IWRITE
        assert untouched.exists()
        assert chmod_calls == []
    finally:
        if blocked.exists():
            real_chmod(blocked, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_target_replaced_after_state_read_is_caught_before_chmod(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    blocked = put(target, "a-blocked.md", b"previewed target")
    untouched = put(target, "z-untouched.md", b"later deletion must not run")
    blocked.chmod(stat.S_IREAD)
    plan = analyze(engine, source, target)
    real_chmod = engine_module.os.chmod
    real_snapshot = engine_module.snapshot
    real_unlink = engine_module.os.unlink
    state = {"denied": False, "replaced": False}
    chmod_calls = []

    def denied_unlink(path):
        if paths_module.canonical(path) == paths_module.canonical(blocked):
            state["denied"] = True
            raise PermissionError(errno.EACCES, "simulated access denied", path, 5)
        return real_unlink(path)

    def snapshot_then_replace(path):
        observed = real_snapshot(path)
        if (state["denied"] and not state["replaced"]
                and paths_module.canonical(path) == paths_module.canonical(blocked)):
            state["replaced"] = True
            real_chmod(path, stat.S_IWRITE)
            real_unlink(path)
            Path(paths_module.canonical(path)).write_bytes(b"external replacement")
            real_chmod(path, stat.S_IREAD)
        return observed

    def record_chmod(*args, **kwargs):
        chmod_calls.append((args, kwargs))
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(engine_module.os, "unlink", denied_unlink)
    monkeypatch.setattr(engine_module, "snapshot", snapshot_then_replace)
    monkeypatch.setattr(engine_module.os, "chmod", record_chmod)
    try:
        result = engine.execute(plan)

        assert result.status == "failed"
        assert state["replaced"]
        assert blocked.read_bytes() == b"external replacement"
        assert not blocked.stat().st_mode & stat.S_IWRITE
        assert untouched.exists()
        assert chmod_calls == []
    finally:
        if blocked.exists():
            real_chmod(blocked, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="DOS read-only attributes are Windows-specific")
def test_target_replaced_at_final_attribute_check_is_not_retried(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "keep.md", b"stable content")
    put(target, "keep.md", b"stable content")
    blocked = put(target, "a-blocked.md", b"previewed target")
    untouched = put(target, "z-untouched.md", b"later deletion must not run")
    blocked.chmod(stat.S_IREAD)
    plan = analyze(engine, source, target)
    real_chmod = engine_module.os.chmod
    real_lstat = engine_module.os.lstat
    real_unlink = engine_module.os.unlink
    state = {"cleared": False, "post_clear_lstats": 0}
    operation_payloads = []
    chmod_payloads = []

    def denied_unlink(path):
        if paths_module.canonical(path) == paths_module.canonical(blocked):
            operation_payloads.append(Path(paths_module.canonical(path)).read_bytes())
            raise PermissionError(errno.EACCES, "simulated access denied", path, 5)
        return real_unlink(path)

    def record_chmod(path, mode):
        if (paths_module.canonical(path) == paths_module.canonical(blocked)
                and mode & stat.S_IWRITE):
            chmod_payloads.append(Path(paths_module.canonical(path)).read_bytes())
            state["cleared"] = True
        return real_chmod(path, mode)

    def replace_during_final_lstat(path, *args, **kwargs):
        if paths_module.canonical(path) == paths_module.canonical(blocked) and state["cleared"]:
            state["post_clear_lstats"] += 1
            if state["post_clear_lstats"] == 2:
                real_unlink(path)
                Path(paths_module.canonical(path)).write_bytes(b"external replacement")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(engine_module.os, "unlink", denied_unlink)
    monkeypatch.setattr(engine_module.os, "chmod", record_chmod)
    monkeypatch.setattr(engine_module.os, "lstat", replace_during_final_lstat)
    try:
        result = engine.execute(plan)

        assert result.status == "failed"
        assert operation_payloads == [b"previewed target"]
        assert chmod_payloads == [b"previewed target"]
        assert blocked.read_bytes() == b"external replacement"
        assert blocked.stat().st_mode & stat.S_IWRITE
        assert untouched.exists()
    finally:
        if blocked.exists():
            real_chmod(blocked, stat.S_IWRITE)


def test_source_wins_even_when_target_has_newer_timestamp(mirror):
    engine, source, target = mirror
    source_file = put(source, "note.md", b"source version")
    target_file = put(target, "note.md", b"target version")
    os.utime(source_file, ns=(1_600_000_000_000_000_000,) * 2)
    os.utime(target_file, ns=(1_700_000_000_000_000_000,) * 2)

    plan, result = synchronize(engine, source, target)

    assert plan.counts["update"] == 1
    assert result.copied_files == 1
    assert target_file.read_bytes() == b"source version"


def test_unchanged_second_sync_writes_zero_bytes(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"unchanged note")
    put(source, "video.bin", bytes(range(256)) * 8192)
    synchronize(engine, source, target)

    plan, result = synchronize(engine, source, target)

    assert plan.counts["skip"] == 2
    assert plan.bytes_to_copy == 0
    assert result.copied_files == 0
    assert result.copied_bytes == 0
    assert result.deleted_files == 0


def test_adopts_existing_identical_copy_without_rewriting_or_changing_timestamps(mirror):
    engine, source, target = mirror
    payload = b"Existing immutable video data" * 4096
    source_file = put(source, "电影.mp4", payload)
    target_file = put(target, "电影.mp4", payload)
    os.utime(source_file, ns=(1_700_000_000_123_456_700,) * 2)
    os.utime(target_file, ns=(1_700_000_002_000_000_000,) * 2)
    initial_stat = target_file.stat()

    plan, result = synchronize(engine, source, target)
    second_plan, second_result = synchronize(engine, source, target)

    assert plan.counts["skip"] == 1
    assert result.copied_bytes == 0
    assert second_plan.counts["skip"] == 1
    assert second_result.copied_bytes == 0
    assert target_file.stat().st_mtime_ns == initial_stat.st_mtime_ns
    assert target_file.stat().st_ino == initial_stat.st_ino


def test_renaming_becomes_add_and_delete(mirror):
    engine, source, target = mirror
    put(source, "old.md", b"same content")
    synchronize(engine, source, target)
    (source / "old.md").rename(source / "new.md")

    plan, _ = synchronize(engine, source, target)

    assert plan.counts["add"] == 1
    assert plan.counts["delete"] == 1
    assert not (target / "old.md").exists()
    assert (target / "new.md").read_bytes() == b"same content"


@pytest.mark.parametrize("changed_side", ["source", "target"])
def test_deep_scan_detects_changes_with_same_size_and_mtime(mirror, changed_side):
    engine, source, target = mirror
    put(source, "note.md", b"original")
    synchronize(engine, source, target)
    changed = (source if changed_side == "source" else target) / "note.md"
    old_stat = changed.stat()
    changed.write_bytes(b"modified")
    os.utime(changed, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    deep_plan = analyze(engine, source, target, deep=True)

    assert deep_plan.can_execute, deep_plan.errors
    assert deep_plan.counts["update"] == 1
    result = engine.execute(deep_plan)
    assert not result.errors
    assert (target / "note.md").read_bytes() == (source / "note.md").read_bytes()


def test_different_volumes_hash_both_sides_concurrently_on_analysis(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "video.mp4", b"same content")
    put(target, "video.mp4", b"same content")
    pretend_separate_volumes(monkeypatch, source, target)
    barrier = Barrier(2)
    worker_threads = set()
    worker_lock = Lock()

    def overlapping_hash(path, expected, cancel, progress, relative):
        with worker_lock:
            worker_threads.add(get_ident())
        barrier.wait(timeout=3)
        progress(engine_module.Progress(
            "hash", relative_path=relative,
            completed_bytes=expected["size"], total_bytes=expected["size"],
        ))
        return "same-digest"

    monkeypatch.setattr(engine_module, "_hash_file", overlapping_hash)
    callback_threads = []
    hash_progress = []
    save_threads = []
    caller_thread = get_ident()
    real_save = BaselineStore.save

    def record_save(self, *args, **kwargs):
        save_threads.append(get_ident())
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(BaselineStore, "save", record_save)

    def record_progress(event):
        callback_threads.append(get_ident())
        if event.phase == "hash":
            hash_progress.append((event.completed_bytes, event.total_bytes))

    plan = analyze(
        engine, source, target, deep=True,
        progress=record_progress,
    )

    assert plan.can_execute, plan.errors
    assert plan.counts["skip"] == 1
    assert len(worker_threads) == 2
    assert callback_threads
    assert set(callback_threads) == {caller_thread}, "Public progress must stay on the analysis thread"
    assert save_threads == [caller_thread], "SQLite writes must stay on the analysis thread"
    assert [value for value, _ in hash_progress] == sorted(value for value, _ in hash_progress)
    assert hash_progress[-1] == (2 * len(b"same content"), 2 * len(b"same content"))
    assert not any(thread.name.startswith("obmanage-hash") for thread in enumerate_threads())


def test_parallel_hash_failure_stops_peer_before_analysis_returns(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "video.mp4", b"same content")
    put(target, "video.mp4", b"same content")
    pretend_separate_volumes(monkeypatch, source, target)
    barrier = Barrier(2)
    peer_finished = Event()

    def failing_hash(path, expected, cancel, progress, relative):
        barrier.wait(timeout=3)
        if os.path.commonpath((paths_module.canonical(path), str(source))) == str(source):
            raise PermissionError("simulated source read failure")
        while not cancel.is_set():
            Event().wait(0.001)
        peer_finished.set()
        raise SyncCancelled("peer stopped")

    monkeypatch.setattr(engine_module, "_hash_file", failing_hash)

    plan = analyze(engine, source, target, deep=True)

    assert not plan.can_execute
    assert any("simulated source read failure" in error for error in plan.errors)
    assert peer_finished.is_set(), "No hash worker may outlive analyze()"
    store = BaselineStore(engine.state_dir)
    try:
        assert store.records(plan.pair_id) == {}
    finally:
        store.close()
    assert not any(thread.name.startswith("obmanage-hash") for thread in enumerate_threads())


def test_external_cancel_joins_both_parallel_hash_workers(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "video.mp4", b"same content")
    put(target, "video.mp4", b"same content")
    pretend_separate_volumes(monkeypatch, source, target)
    barrier = Barrier(2)
    cancelled = Event()
    pause = Event()
    finished = 0
    finished_lock = Lock()
    all_finished = Event()

    def cancellable_hash(path, expected, cancel, progress, relative):
        nonlocal finished
        barrier.wait(timeout=3)
        if os.path.commonpath((paths_module.canonical(path), str(source))) == str(source):
            cancelled.set()
        while not cancel.is_set():
            pause.wait(0.001)
        with finished_lock:
            finished += 1
            if finished == 2:
                all_finished.set()
        raise SyncCancelled("parallel hash cancelled")

    monkeypatch.setattr(engine_module, "_hash_file", cancellable_hash)

    with pytest.raises(SyncCancelled):
        analyze(engine, source, target, deep=True, cancel=cancelled)

    assert all_finished.is_set()
    assert not any(thread.name.startswith("obmanage-hash") for thread in enumerate_threads())


def test_unchanged_deep_verification_reestablishes_retired_baseline(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "video.mp4", b"same content" * 1024)
    synchronize(engine, source, target)
    real_save = BaselineStore.save
    saves = []

    def record_save(self, *args, **kwargs):
        saves.append(args)
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(BaselineStore, "save", record_save)

    plan = analyze(engine, source, target, deep=True)

    assert plan.can_execute, plan.errors
    assert plan.counts["skip"] == 1
    assert len(saves) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata permits this deep-check scenario")
def test_deep_mismatch_invalidates_forward_and_reverse_baselines(mirror):
    engine, source, target = mirror
    source_file = put(source, "note.md", b"AAAA")
    target_file = put(target, "note.md", b"AAAA")
    forward = analyze(engine, source, target)
    assert forward.counts["skip"] == 1
    reverse = analyze(engine, target, source)
    assert reverse.counts["skip"] == 1
    old_target = paths_module.snapshot(target_file)
    target_file.write_bytes(b"BBBB")
    os.utime(target_file, ns=(old_target["mtime_ns"], old_target["mtime_ns"]))
    assert paths_module.snapshot(target_file) == old_target

    deep = analyze(engine, source, target, deep=True)

    assert deep.can_execute, deep.errors
    assert deep.counts["update"] == 1
    store = BaselineStore(engine.state_dir)
    try:
        assert store.records(forward.pair_id) == {}
        assert store.records(reverse.pair_id) == {}
    finally:
        store.close()
    assert analyze(engine, source, target).counts["update"] == 1
    assert analyze(engine, target, source).counts["update"] == 1
    assert source_file.read_bytes() == b"AAAA"
    assert target_file.read_bytes() == b"BBBB"


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata permits this cache-revival scenario")
def test_observed_size_mismatch_cannot_revive_an_old_baseline(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"AAAA")
    target_file = put(target, "note.md", b"AAAA")
    initial = analyze(engine, source, target)
    assert initial.counts["skip"] == 1
    old_target = paths_module.snapshot(target_file)

    target_file.write_bytes(b"wrong")
    mismatch = analyze(engine, source, target)
    assert mismatch.counts["update"] == 1
    target_file.write_bytes(b"CCCC")
    os.utime(target_file, ns=(old_target["mtime_ns"], old_target["mtime_ns"]))
    assert paths_module.snapshot(target_file) == old_target

    revisited = analyze(engine, source, target)

    assert revisited.counts["update"] == 1
    store = BaselineStore(engine.state_dir)
    try:
        assert store.records(initial.pair_id) == {}
    finally:
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata permits this deep-check scenario")
def test_deep_check_retires_old_claim_before_hashing(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "note.md", b"AAAA")
    target_file = put(target, "note.md", b"AAAA")
    initial = analyze(engine, source, target)
    assert initial.counts["skip"] == 1
    old_target = paths_module.snapshot(target_file)
    target_file.write_bytes(b"BBBB")
    os.utime(target_file, ns=(old_target["mtime_ns"], old_target["mtime_ns"]))
    assert paths_module.snapshot(target_file) == old_target
    store = BaselineStore(engine.state_dir)
    store.connection.execute("""
        CREATE TRIGGER reject_deep_invalidation
        BEFORE DELETE ON baselines
        BEGIN
            SELECT RAISE(ABORT, 'simulated invalidation failure');
        END;
    """)
    store.connection.commit()
    store.close()
    real_hash = engine_module._hash_file

    def hash_must_not_start(*args, **kwargs):
        pytest.fail("Deep hashing must not start while an old claim is still active")

    monkeypatch.setattr(engine_module, "_hash_file", hash_must_not_start)
    failed = analyze(engine, source, target, deep=True)
    assert not failed.can_execute
    assert any("simulated invalidation failure" in error for error in failed.errors)

    store = BaselineStore(engine.state_dir)
    store.connection.execute("DROP TRIGGER reject_deep_invalidation")
    store.connection.commit()
    store.close()
    monkeypatch.setattr(engine_module, "_hash_file", real_hash)

    deep = analyze(engine, source, target, deep=True)
    assert deep.can_execute, deep.errors
    assert deep.counts["update"] == 1
    assert analyze(engine, source, target).counts["update"] == 1


def test_analysis_does_not_write_or_delete_vault_files(mirror):
    engine, source, target = mirror
    put(source, "new.md", b"new source")
    put(source, "update.md", b"correct source")
    put(target, "update.md", b"old target")
    put(target, "remove.md", b"target only")
    (source / "empty").mkdir()

    plan = analyze(engine, source, target)

    assert plan.can_execute, plan.errors
    assert not (target / "new.md").exists()
    assert not (target / "empty").exists()
    assert (target / "update.md").read_bytes() == b"old target"
    assert (target / "remove.md").read_bytes() == b"target only"


@pytest.mark.parametrize("changed_side", ["source", "target"])
def test_preview_becomes_invalid_after_file_change_and_never_deletes(mirror, changed_side):
    engine, source, target = mirror
    put(source, "note.md", b"source")
    put(target, "note.md", b"target")
    put(target, "target-only.md", b"must survive stale plan")
    plan = analyze(engine, source, target)
    changed_root = source if changed_side == "source" else target
    put(changed_root, "note.md", b"modified after preview")

    require_rejected(lambda: engine.execute(plan))

    assert (target / "target-only.md").read_bytes() == b"must survive stale plan"
    if changed_side == "source":
        assert (target / "note.md").read_bytes() == b"target"
    else:
        assert (target / "note.md").read_bytes() == b"modified after preview"


def test_new_source_file_after_preview_prevents_target_deletion(mirror):
    engine, source, target = mirror
    put(source, "present.md", b"present")
    put(target, "returned.md", b"old target")
    plan = analyze(engine, source, target)
    put(source, "returned.md", b"restored since preview")

    require_rejected(lambda: engine.execute(plan))

    assert (target / "returned.md").read_bytes() == b"old target"


def test_missing_source_is_never_treated_as_empty(mirror):
    engine, source, target = mirror
    put(target, "keep.md", b"do not delete")
    source.rmdir()

    require_rejected(lambda: analyze(engine, source, target))

    assert (target / "keep.md").read_bytes() == b"do not delete"


def test_empty_source_requires_explicit_allow_empty(mirror):
    engine, source, target = mirror
    put(target, "old/note.md", b"remove after confirmation")
    plan = analyze(engine, source, target)
    assert plan.source_empty

    require_rejected(lambda: engine.execute(plan))
    assert (target / "old/note.md").exists()

    confirmed_plan = analyze(engine, source, target)
    result = engine.execute(confirmed_plan, allow_empty=True)
    assert not result.errors, result.errors
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("source_is_directory", [True, False])
def test_file_directory_conflict_blocks_execution_and_all_deletion(mirror, source_is_directory):
    engine, source, target = mirror
    directory_root, file_root = (source, target) if source_is_directory else (target, source)
    put(directory_root, "conflict/child.md", b"child")
    put(file_root, "conflict", b"file")
    put(target, "target-only.md", b"keep on conflict")

    plan = analyze(engine, source, target)

    assert not plan.can_execute
    assert plan.errors or plan.counts["error"]
    require_rejected(lambda: engine.execute(plan))
    assert (target / "target-only.md").read_bytes() == b"keep on conflict"
    assert (directory_root / "conflict/child.md").read_bytes() == b"child"
    assert (file_root / "conflict").read_bytes() == b"file"


def test_pre_cancelled_execution_keeps_existing_target(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"new source")
    put(target, "note.md", b"old target")
    put(target, "keep.md", b"keep on cancellation")
    plan = analyze(engine, source, target)
    cancelled = Event()
    cancelled.set()

    require_rejected(lambda: engine.execute(plan, cancel=cancelled))

    assert (target / "note.md").read_bytes() == b"old target"
    assert (target / "keep.md").read_bytes() == b"keep on cancellation"


def test_each_target_has_independent_comparison_records(tmp_path):
    engine = SyncEngine(tmp_path / "state")
    source, first, second = [tmp_path / name for name in ("source", "first", "second")]
    for folder in (source, first, second):
        folder.mkdir()
    put(source, "note.md", b"source content")
    synchronize(engine, source, first)
    put(second, "note.md", b"wrong contents")

    plan, result = synchronize(engine, source, second)

    assert plan.counts["update"] == 1
    assert result.copied_files == 1
    assert (first / "note.md").read_bytes() == b"source content"
    assert (second / "note.md").read_bytes() == b"source content"


def test_hash_baseline_persists_across_engine_instances(tmp_path):
    source, target, state = [tmp_path / name for name in ("source", "target", "state")]
    source.mkdir()
    target.mkdir()
    put(source, "video.mp4", b"constant video payload")
    synchronize(SyncEngine(state), source, target)

    plan, result = synchronize(SyncEngine(state), source, target)

    assert plan.counts["skip"] == 1
    assert result.copied_bytes == 0


def test_unchanged_baseline_avoids_reading_large_file_contents(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "video.mp4", b"video payload" * 1024)
    synchronize(engine, source, target)

    def unnecessary_hash(*args, **kwargs):
        pytest.fail("An unchanged cached file must not be read for SHA-256 again")

    monkeypatch.setattr(engine_module, "_hash_file", unnecessary_hash)

    plan, result = synchronize(engine, source, target)

    assert plan.counts["skip"] == 1
    assert result.copied_bytes == 0


def test_cancelled_first_adoption_reuses_already_hashed_record(mirror, monkeypatch):
    engine, source, target = mirror
    for name in ("first.mp4", "second.mp4", "third.mp4"):
        put(source, name, b"video" * 4096)
        put(target, name, b"video" * 4096)
    cancelled = Event()
    completed = []

    def stop_after_first(event):
        if event.phase == "compare" and event.completed_files == 1:
            completed.append(event.relative_path)
            cancelled.set()

    require_rejected(lambda: analyze(engine, source, target, cancel=cancelled, progress=stop_after_first))
    assert len(completed) == 1
    original_hash = engine_module._hash_file
    hashed_paths = []

    def count_hash(path, *args, **kwargs):
        hashed_paths.append(str(path))
        return original_hash(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_hash_file", count_hash)
    resumed = analyze(engine, source, target)

    assert resumed.can_execute, resumed.errors
    assert resumed.counts["skip"] == 3
    assert len(hashed_paths) == 4, "Only the two not-yet-adopted files should be read on both sides"
    assert not any(Path(path).name == Path(completed[0]).name for path in hashed_paths)


def test_target_metadata_change_rehashes_and_repairs_content(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"source content")
    synchronize(engine, source, target)
    put(target, "note.md", b"changed externally on target")

    plan, result = synchronize(engine, source, target)

    assert plan.counts["update"] == 1
    assert result.copied_files == 1
    assert (target / "note.md").read_bytes() == b"source content"


def test_cancel_after_copied_file_prevents_all_deletions_and_can_resume(mirror):
    engine, source, target = mirror
    put(source, "a.md", b"first copied file")
    put(source, "b.md", b"second copied file")
    put(target, "obsolete.md", b"keep until copies complete")
    plan = analyze(engine, source, target)
    cancelled = Event()

    def stop_after_copy(event):
        if event.phase == "copied":
            cancelled.set()

    require_rejected(lambda: engine.execute(plan, cancel=cancelled, progress=stop_after_copy))

    assert (target / "obsolete.md").read_bytes() == b"keep until copies complete"
    copied = [path for path in target.iterdir() if path.name in {"a.md", "b.md"}]
    assert len(copied) == 1
    _, resumed = synchronize(engine, source, target)
    assert resumed.copied_files == 1
    assert not (target / "obsolete.md").exists()


def test_insufficient_space_keeps_existing_target_and_obsolete_files(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "note.md", b"new source with more bytes")
    put(target, "note.md", b"old target")
    put(target, "obsolete.md", b"do not delete")
    plan = analyze(engine, source, target)
    monkeypatch.setattr(engine_module.shutil, "disk_usage", lambda path: SimpleNamespace(total=1000, used=1000, free=0))

    require_rejected(lambda: engine.execute(plan))

    assert (target / "note.md").read_bytes() == b"old target"
    assert (target / "obsolete.md").read_bytes() == b"do not delete"


def test_replace_failure_preserves_previous_file_and_stops_deletion(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "note.md", b"new source")
    put(target, "note.md", b"old target")
    put(target, "obsolete.md", b"do not delete")
    plan = analyze(engine, source, target)

    def locked_replace(*args, **kwargs):
        raise PermissionError("simulated target file in use")

    monkeypatch.setattr(engine_module.os, "replace", locked_replace)

    require_rejected(lambda: engine.execute(plan))

    assert (target / "note.md").read_bytes() == b"old target"
    assert (target / "obsolete.md").read_bytes() == b"do not delete"
    assert sorted(path.name for path in target.iterdir()) == ["note.md", "obsolete.md"]


def test_reissued_file_identity_after_replace_is_verified_and_accepted(mirror, monkeypatch):
    engine, source, target = mirror
    payload = b"new source content"
    put(source, "note.md", payload)
    put(target, "obsolete.md", b"delete only after the committed file is verified")
    plan = analyze(engine, source, target)
    simulate_reissued_staging_identity(monkeypatch)
    real_hash = engine_module._hash_file
    hashed_paths = []

    def record_hash(path, *args, **kwargs):
        hashed_paths.append(paths_module.canonical(path))
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_hash_file", record_hash)

    result = engine.execute(plan)

    assert result.status == "success", result.errors
    assert (target / "note.md").read_bytes() == payload
    assert not (target / "obsolete.md").exists()
    assert paths_module.canonical(target / "note.md") in hashed_paths

    def unnecessary_hash(*args, **kwargs):
        pytest.fail("The verified committed snapshot must be saved as the baseline")

    monkeypatch.setattr(engine_module, "_hash_file", unnecessary_hash)
    next_plan = analyze(engine, source, target)
    assert next_plan.can_execute, next_plan.errors
    assert next_plan.counts["skip"] == 1
    assert next_plan.bytes_to_copy == 0


def test_reissued_identity_does_not_accept_wrong_committed_content(mirror, monkeypatch):
    engine, source, target = mirror
    payload = b"expected source bytes"
    source_file = put(source, "note.md", payload)
    put(target, "obsolete.md", b"must survive a failed committed-file check")
    plan = analyze(engine, source, target)
    simulate_reissued_staging_identity(monkeypatch)
    real_replace = engine_module.os.replace

    def replace_then_corrupt(staged, destination):
        real_replace(staged, destination)
        Path(paths_module.canonical(destination)).write_bytes(b"X" * len(payload))
        source_stat = source_file.stat()
        engine_module.os.utime(
            destination, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns)
        )

    monkeypatch.setattr(engine_module.os, "replace", replace_then_corrupt)

    result = engine.execute(plan)

    assert result.status == "failed"
    assert any("提交后目标文件发生变化" in error for error in result.errors)
    assert (target / "obsolete.md").read_bytes() == b"must survive a failed committed-file check"


def test_source_modified_while_copying_never_replaces_target(mirror):
    engine, source, target = mirror
    source_file = put(source, "video.bin", b"new source" * 1_000_000)
    put(target, "video.bin", b"old target")
    put(target, "obsolete.md", b"do not delete")
    plan = analyze(engine, source, target)
    modified = []

    def mutate_during_copy(event):
        if event.phase == "copy" and not modified:
            modified.append(True)
            with source_file.open("ab") as handle:
                handle.write(b"source changed while being copied")

    require_rejected(lambda: engine.execute(plan, progress=mutate_during_copy))

    assert modified, "Test must modify the source during a real copy"
    assert (target / "video.bin").read_bytes() == b"old target"
    assert (target / "obsolete.md").read_bytes() == b"do not delete"


def test_replaced_volume_invalidates_preview_without_touching_target(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "note.md", b"new source")
    put(target, "note.md", b"old target")
    put(target, "obsolete.md", b"do not delete")
    plan = analyze(engine, source, target)
    original_identity = paths_module.volume_identity
    monkeypatch.setattr(paths_module, "volume_identity", lambda path: original_identity(path) + ":replacement-volume")

    require_rejected(lambda: engine.execute(plan))

    assert (target / "note.md").read_bytes() == b"old target"
    assert (target / "obsolete.md").read_bytes() == b"do not delete"


def test_disconnection_after_copy_prevents_deletion(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "note.md", b"new source")
    put(target, "obsolete.md", b"do not delete")
    plan = analyze(engine, source, target)

    def disconnected(*args, **kwargs):
        raise SyncError("simulated disk disconnected")

    def disconnect_after_copy(event):
        if event.phase == "copied":
            monkeypatch.setattr(paths_module, "volume_identity", disconnected)

    require_rejected(lambda: engine.execute(plan, progress=disconnect_after_copy))

    assert (target / "obsolete.md").read_bytes() == b"do not delete"


def test_unreadable_source_during_analysis_blocks_execution(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "note.md", b"new source")
    put(target, "note.md", b"old target")
    put(target, "obsolete.md", b"do not delete")

    def unreadable_source(*args, **kwargs):
        raise PermissionError("simulated source file is exclusively locked")

    monkeypatch.setattr(engine_module, "_hash_file", unreadable_source)

    plan = analyze(engine, source, target)

    assert not plan.can_execute
    assert any("locked" in message for message in plan.errors)
    require_rejected(lambda: engine.execute(plan))
    assert (target / "note.md").read_bytes() == b"old target"
    assert (target / "obsolete.md").read_bytes() == b"do not delete"


def test_mid_write_disk_full_cleans_staging_file_without_losing_previous_version(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "note.md", b"new source data" * 1024)
    put(target, "note.md", b"old target")
    put(target, "obsolete.md", b"do not delete")
    plan = analyze(engine, source, target)
    real_fdopen = engine_module.os.fdopen
    partial_writes = []

    class FailsAfterPartialWrite:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def write(self, data):
            self.wrapped.write(data[:32])
            partial_writes.append(True)
            raise OSError(errno.ENOSPC, "simulated disk full during write")

    def failing_fdopen(*args, **kwargs):
        return FailsAfterPartialWrite(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(engine_module.os, "fdopen", failing_fdopen)

    require_rejected(lambda: engine.execute(plan))

    assert partial_writes, "Test must fail after a staging file has actually received bytes"
    assert (target / "note.md").read_bytes() == b"old target"
    assert (target / "obsolete.md").read_bytes() == b"do not delete"
    assert sorted(path.name for path in target.iterdir()) == ["note.md", "obsolete.md"]


def test_source_change_after_copy_stops_cleanup_phase(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"new source")
    put(target, "obsolete.md", b"do not delete")
    plan = analyze(engine, source, target)

    def change_source_after_copy(event):
        if event.phase == "copied":
            put(source, "arrived.md", b"added while synchronization was running")

    require_rejected(lambda: engine.execute(plan, progress=change_source_after_copy))

    assert (target / "note.md").read_bytes() == b"new source"
    assert (target / "obsolete.md").read_bytes() == b"do not delete"
    assert not (target / "arrived.md").exists()


def test_overlapping_engine_operation_is_rejected(mirror):
    engine, source, target = mirror
    put(source, "note.md", b"source")
    simultaneous = []

    def attempt_reentry(event):
        if event.phase == "compare" and not simultaneous:
            with pytest.raises(SyncError):
                engine.analyze(str(source), str(target))
            simultaneous.append(True)

    plan = analyze(engine, source, target, progress=attempt_reentry)

    assert simultaneous
    assert plan.can_execute, plan.errors
    assert not engine.execute(plan).errors


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison is case-insensitive")
def test_case_only_filename_differences_are_not_deleted_or_copied(mirror):
    engine, source, target = mirror
    put(source, "Notes/Example.md", b"same note")
    put(target, "notes/example.md", b"same note")

    plan, result = synchronize(engine, source, target)

    assert plan.counts["skip"] == 1
    assert plan.counts["add"] == 0
    assert plan.counts["delete"] == 0
    assert result.copied_bytes == 0
    assert (target / "Notes/Example.md").read_bytes() == b"same note"


@pytest.mark.parametrize("target_change", ["skipped", "copied", "new_file"])
def test_target_change_after_copy_blocks_cleanup_and_never_reports_success(mirror, target_change):
    engine, source, target = mirror
    put(source, "stable.md", b"unchanged content")
    synchronize(engine, source, target)
    put(source, "new.md", b"new source content")
    put(target, "obsolete.md", b"keep until all preconditions hold")
    plan = analyze(engine, source, target)

    def change_target_after_copy(event):
        if event.phase == "copied":
            relative = {"skipped": "stable.md", "copied": "new.md", "new_file": "unexpected.md"}[target_change]
            put(target, relative, b"modified outside the application during synchronization")

    result = engine.execute(plan, progress=change_target_after_copy)

    assert result.status != "success"
    assert result.errors
    assert (target / "obsolete.md").read_bytes() == b"keep until all preconditions hold"


@pytest.mark.parametrize("changed_side", ["source", "target"])
def test_unrelated_external_change_during_cleanup_is_reported_as_incomplete(mirror, changed_side):
    engine, source, target = mirror
    put(source, "stable.md", b"unchanged content")
    synchronize(engine, source, target)
    put(target, "obsolete-a.md", b"obsolete A")
    put(target, "obsolete-b.md", b"obsolete B")
    plan = analyze(engine, source, target)
    changed = []

    def change_after_first_deletion(event):
        if event.phase == "delete" and not changed:
            changed.append(True)
            put(source if changed_side == "source" else target, "stable.md", b"external modification while cleanup is running")

    result = engine.execute(plan, progress=change_after_first_deletion)

    assert changed
    assert result.status != "success"
    assert result.errors
    changed_file = (source if changed_side == "source" else target) / "stable.md"
    assert changed_file.read_bytes() == b"external modification while cleanup is running"
    # Each unchanged target-only candidate remains independently safe to delete.
    # A final whole-manifest check must report the unrelated retained-file change.


@pytest.mark.parametrize("changed_side", ["source", "target"])
def test_candidate_change_during_cleanup_stops_that_and_remaining_deletions(mirror, changed_side):
    engine, source, target = mirror
    put(source, "stable.md", b"unchanged content")
    synchronize(engine, source, target)
    for name in ("obsolete-a.md", "obsolete-b.md", "obsolete-c.md"):
        put(target, name, b"obsolete original")
    plan = analyze(engine, source, target)
    candidates = [item.relative_path for item in plan.items if item.action == "delete"]
    changed = []

    def change_next_candidate_after_first_deletion(event):
        if event.phase == "delete" and not changed:
            changed.append(True)
            assert event.relative_path == candidates[0]
            put(source if changed_side == "source" else target, candidates[1], b"candidate changed during cleanup")

    result = engine.execute(plan, progress=change_next_candidate_after_first_deletion)

    assert changed
    assert result.status != "success"
    assert result.errors
    assert not (target / candidates[0]).exists()
    assert (target / candidates[1]).exists()
    assert (target / candidates[2]).read_bytes() == b"obsolete original"
    expected = b"obsolete original" if changed_side == "source" else b"candidate changed during cleanup"
    assert (target / candidates[1]).read_bytes() == expected


@pytest.mark.parametrize("changed_side", ["source", "target"])
def test_change_after_last_deletion_is_not_reported_as_success(mirror, changed_side):
    engine, source, target = mirror
    put(source, "stable.md", b"unchanged content")
    synchronize(engine, source, target)
    put(target, "obsolete.md", b"obsolete")
    plan = analyze(engine, source, target)

    def change_after_deletion(event):
        if event.phase == "delete":
            put(source if changed_side == "source" else target, "stable.md", b"external modification after the last deletion")

    result = engine.execute(plan, progress=change_after_deletion)

    assert not (target / "obsolete.md").exists()
    assert result.status != "success"
    assert result.errors


def register_crash_temp(engine, source, target, temp, *, mismatched_identity=False):
    """Simulate the exact durable registration left behind by a killed process."""
    plan = analyze(engine, source, target)
    assert plan.can_execute, plan.errors
    recorded_identity = paths_module.identity(paths_module.snapshot(temp))
    if mismatched_identity:
        recorded_identity = (*recorded_identity[:-1], -1)
    store = BaselineStore(engine.state_dir)
    try:
        store.register_temp(plan.pair_id, paths_module.native(temp), recorded_identity)
    finally:
        store.close()


def test_registered_crash_temp_is_reclaimed_before_checking_available_capacity(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "new.md", b"new source content")
    crash_temp = put(target, ".obmanage-crash.tmp", b"partial staging content")
    ordinary_extra = put(target, "ordinary-target-only.md", b"do not reclaim early")
    register_crash_temp(engine, source, target, crash_temp)
    plan = analyze(engine, source, target)
    capacity_checks = []

    def disk_usage_after_reclamation(path):
        capacity_checks.append(True)
        assert not crash_temp.exists(), "Owned crash residue must be reclaimed before the free-space check"
        assert ordinary_extra.exists(), "Ordinary mirror deletions must wait until all copies succeed"
        return SimpleNamespace(total=1_000_000, used=0, free=1_000_000)

    monkeypatch.setattr(engine_module.shutil, "disk_usage", disk_usage_after_reclamation)
    result = engine.execute(plan)

    assert capacity_checks
    assert result.status == "success", result.errors
    assert result.deleted_files == 2
    assert (target / "new.md").read_bytes() == b"new source content"
    assert not crash_temp.exists()
    assert not ordinary_extra.exists()
    store = BaselineStore(engine.state_dir)
    try:
        assert store.temps(plan.pair_id) == []
    finally:
        store.close()


@pytest.mark.parametrize("registration", ["unregistered", "wrong_identity", "present_in_source"])
def test_early_crash_cleanup_never_deletes_an_unowned_or_source_named_file(mirror, monkeypatch, registration):
    engine, source, target = mirror
    put(source, "new.md", b"new source content")
    possible_temp = put(target, ".obmanage-crash.tmp", b"target file to protect")
    if registration == "present_in_source":
        put(source, ".obmanage-crash.tmp", b"target file to protect")
    if registration != "unregistered":
        register_crash_temp(engine, source, target, possible_temp, mismatched_identity=registration == "wrong_identity")
    plan = analyze(engine, source, target)
    monkeypatch.setattr(engine_module.shutil, "disk_usage", lambda path: SimpleNamespace(total=1, used=1, free=0))

    result = engine.execute(plan)

    assert result.status != "success"
    assert possible_temp.read_bytes() == b"target file to protect"
    assert not (target / "new.md").exists()


def test_stale_preview_prevents_even_registered_temp_reclamation(mirror):
    engine, source, target = mirror
    put(source, "new.md", b"source at preview")
    crash_temp = put(target, ".obmanage-crash.tmp", b"partial staging content")
    register_crash_temp(engine, source, target, crash_temp)
    plan = analyze(engine, source, target)
    put(source, "new.md", b"changed after preview")

    result = engine.execute(plan)

    assert result.status != "success"
    assert crash_temp.read_bytes() == b"partial staging content"


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison is case-insensitive")
def test_case_only_rename_changes_actual_names_and_refreshes_baseline(mirror, monkeypatch):
    engine, source, target = mirror
    put(source, "Notes/Topics/Example.md", b"same note")
    put(target, "notes/topics/example.md", b"same note")

    plan, result = synchronize(engine, source, target)

    assert plan.counts["rename"] == 3
    assert result.renamed_items == 3
    assert result.copied_bytes == 0
    assert [path.name for path in target.iterdir()] == ["Notes"]
    assert [path.name for path in (target / "Notes").iterdir()] == ["Topics"]
    assert [path.name for path in (target / "Notes/Topics").iterdir()] == ["Example.md"]

    def unnecessary_hash(*args, **kwargs):
        pytest.fail("Successful case-only rename must preserve a reusable verified baseline")

    monkeypatch.setattr(engine_module, "_hash_file", unnecessary_hash)
    second_plan, second_result = synchronize(engine, source, target)
    assert second_plan.counts["rename"] == 0
    assert second_plan.counts["skip"] == 1
    assert second_result.copied_bytes == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison is case-insensitive")
def test_case_only_rename_and_content_update_both_apply(mirror):
    engine, source, target = mirror
    put(source, "Note.md", b"new source content")
    put(target, "note.md", b"old target")

    plan, result = synchronize(engine, source, target)

    assert plan.counts["rename"] == 1
    assert plan.counts["update"] == 1
    assert result.renamed_items == 1
    assert result.copied_files == 1
    assert [path.name for path in target.iterdir()] == ["Note.md"]
    assert (target / "Note.md").read_bytes() == b"new source content"


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison is case-insensitive")
def test_case_only_rename_after_previous_sync_keeps_video_bytes_unchanged(mirror):
    engine, source, target = mirror
    put(source, "notes/video.mp4", b"immutable video" * 1024)
    put(source, "note.md", b"note")
    synchronize(engine, source, target)
    video_identity = (target / "notes/video.mp4").stat().st_ino
    (source / "notes").rename(source / "Notes")
    (source / "note.md").rename(source / "Note.md")

    plan, result = synchronize(engine, source, target)

    assert plan.counts["rename"] == 2
    assert result.renamed_items == 2
    assert result.copied_bytes == 0
    assert sorted(path.name for path in target.iterdir()) == ["Note.md", "Notes"]
    assert (target / "Notes/video.mp4").stat().st_ino == video_identity
