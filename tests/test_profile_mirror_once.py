"""All writes use tmp_path, including simulated device identities and console input."""
from __future__ import annotations

import builtins
import csv
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest

import obmanage.engine as engine_module
import obmanage.paths as paths
from obmanage.models import SyncCancelled, SyncError
from obmanage.scanning.metrics import ScanMetrics, count, current, phase
from tools import profile_mirror_once as profile

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Local Windows diagnostic entry")


class Lock:
    def getLockInfo(self):
        return os.getpid(), "", ""

    def isLocked(self):
        return True

    def unlock(self):
        pass


@pytest.fixture
def service(tmp_path, monkeypatch):
    source, parent, work, state = (tmp_path / name for name in ("source", "portable", "work", "state"))
    for path in (source, parent, work, state):
        path.mkdir()
    target = parent / "target"
    original_volume = paths.volume_identity

    def volume(path):
        resolved = Path(paths.canonical(path))
        for name, root in (("source", source), ("target", parent)):
            if resolved.is_relative_to(root):
                return f"\\\\?\\volume{{mock-{name}}}:12345678:NTFS"
        return original_volume(path)

    monkeypatch.setattr(paths, "volume_identity", volume)
    monkeypatch.setattr(profile, "require_direction", lambda *_: None)
    monkeypatch.setattr(profile, "acquire_instance_lock", lambda _: Lock())
    monkeypatch.setattr(profile, "console_available", lambda: True)
    monkeypatch.setattr(profile, "probe_devices", lambda: {
        letter: dict(fs="NTFS", bus=bus, disk=index, offline=False, read_only=False)
        for index, (letter, bus) in enumerate((("E", "NVMe"), ("H", "USB")))})
    report = profile.Report("test-mirror", work, dict(git_commit="test", git_dirty=True,
                          obmanage_version="test", implementation_sha256="test"))
    return profile.MirrorOnce(source, target, state, report, threading.Event(), execute=True, verify_deep=True)


def put(root, name, content=b"private payload"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def manifest(root):
    if not root.exists():
        return None
    return {p.relative_to(root).as_posix(): (paths.snapshot(p), p.read_bytes() if p.is_file() else None)
            for p in (root, *root.rglob("*"))}


def forbid_writes(monkeypatch, *roots):
    def check(path):
        if not isinstance(path, int):
            p = Path(paths.canonical(path))
            assert not any(p.is_relative_to(root) for root in roots), "Live-tree mutation attempted"

    for name, n in (("mkdir", 1), ("makedirs", 1), ("unlink", 1), ("remove", 1), ("rmdir", 1),
                    ("utime", 1), ("chmod", 1), ("rename", 2), ("replace", 2)):
        original = getattr(os, name)

        def wrap(*args, _original=original, _n=n, **kwargs):
            for path in args[:_n]:
                check(path)
            return _original(*args, **kwargs)
        monkeypatch.setattr(os, name, wrap)
    for module in (builtins, io):
        original = module.open

        def opened(path, mode="r", *args, _original=original, **kwargs):
            if any(c in mode for c in "wax+"):
                check(path)
            return _original(path, mode, *args, **kwargs)
        monkeypatch.setattr(module, "open", opened)


def prepared(service, resources):
    service.report.measure("precheck", lambda: service.precheck(resources))
    return service.preview()


def operator(monkeypatch, replies):
    values = iter(replies)
    monkeypatch.setattr(profile, "read_console_phrase", lambda *_: next(values))


def auto_operator(service, monkeypatch):
    def reply(prompt, *_args):
        # Test-only input. The real entry never synthesizes a response.
        return prompt.split("输入 ")[-1].split(":")[0]
    monkeypatch.setattr(profile, "read_console_phrase", reply)


def test_default_dry_run_never_writes_either_endpoint(service, monkeypatch):
    put(service.source, "private-note.md")
    put(service.target, "private-deletion.md", b"old")
    source, target = manifest(service.source), manifest(service.target)
    service.execute_requested = False
    monkeypatch.setattr(service.engine, "execute", lambda *_a, **_k: pytest.fail("execute in dry-run"))
    forbid_writes(monkeypatch, service.source, service.target)
    with ExitStack() as resources, pytest.raises(profile.Blocked, match="DRY_RUN"):
        service.run(resources)
    assert manifest(service.source) == source and manifest(service.target) == target
    assert not service.report.data["execution_performed"]
    assert service.report.data["stages"]["execute"]["duration_ms"] is None
    assert (service.report.work / "private_plan.json").exists()


@pytest.mark.parametrize("failure", ("noninteractive", "mismatch", "refuse_delete", "source_changed", "target_changed", "expired", "plan_changed"))
def test_unconfirmed_or_stale_plan_never_calls_execute(service, monkeypatch, failure):
    note = put(service.source, "private-note.md")
    put(service.target, "private-delete.md", b"old")
    with ExitStack() as resources:
        preview = prepared(service, resources)
        operator(monkeypatch, [preview.phrase, preview.delete_phrase])
        if failure == "noninteractive":
            monkeypatch.setattr(profile, "console_available", lambda: False)
        elif failure == "mismatch":
            operator(monkeypatch, ["no"])
        elif failure == "refuse_delete":
            operator(monkeypatch, [preview.phrase, "no"])
        elif failure == "source_changed":
            note.write_bytes(b"external source update")
        elif failure == "target_changed":
            put(service.target, "external.txt", b"preserve")
        elif failure == "expired":
            preview.created_ns -= profile.MAX_PREVIEW_AGE_NS + 1
        else:
            preview.plan.items[0].size += 1
        before_source, before_target = manifest(service.source), manifest(service.target)
        monkeypatch.setattr(service.engine, "execute", lambda *_a, **_k: pytest.fail("unsafe execute"))
        forbid_writes(monkeypatch, service.source, service.target)
        with pytest.raises((profile.Blocked, SyncCancelled, SyncError)):
            service.confirm(preview)
            service.engine.validate_plan(preview.plan)
        assert manifest(service.source) == before_source
        assert manifest(service.target) == before_target


def test_ordinary_mirror_all_actions_and_both_worker_contexts(service, monkeypatch):
    put(service.source, "new.md", b"new")
    put(service.source, ".obsidian/app.json", b"{}")
    put(service.source, ".trash/saved.md", b"retain source trash")
    put(service.source, "movie.mp4", b"video included")
    put(service.source, "update.md", b"new longer content")
    put(service.target, "update.md", b"old")
    put(service.source, "skip.md", b"equal content")
    put(service.target, "skip.md", b"equal content")
    put(service.source, "Case.md", b"same")
    put(service.target, "case.md", b"same")
    put(service.target, "delete_dir/delete.md", b"old")
    (service.source / "empty").mkdir()
    before = manifest(service.source)
    seen = []
    original_hash = engine_module._hash_file

    def hashed(*args, **kwargs):
        assert current() is not None
        seen.append(threading.current_thread().name)
        return original_hash(*args, **kwargs)
    monkeypatch.setattr(engine_module, "_hash_file", hashed)
    auto_operator(service, monkeypatch)
    # Observe even transient mutations of the source, not just final equality.
    forbid_writes(monkeypatch, service.source)
    with ExitStack() as resources:
        service.run(resources)
    service.report.save()
    data = service.report.data
    assert data["status"] == "SUCCESS_VERIFIED"
    assert data["plan"]["rename_items"] == 1 and data["plan"]["delete_files"] == 1
    assert data["result"]["deleted_dirs"] == data["result"]["deleted_files"] == 1
    assert data["result"]["copied_files"] == 5
    assert data["plan"]["skip_files"] == 2
    assert manifest(service.source) == before
    assert {p.name for p in service.target.iterdir()} == {p.name for p in service.source.iterdir()}
    assert data["verification"]["quick"]["status"] == data["verification"]["deep"]["status"] == "PASSED"
    assert data["verification"]["independent_audit"]["status"] == "NOT_RUN"
    assert any(name.startswith("obmanage-hash") for name in seen)
    metrics = data["stages"]["execute"]["metrics"]
    assert metrics["counts"]["source_read_bytes"] == metrics["counts"]["stage_write_bytes"] == data["result"]["copied_bytes"]
    assert metrics["scopes"]["staged_sha256"]["hash_bytes"] == data["result"]["copied_bytes"]
    assert metrics["scopes"]["source_to_stage"]["copy_started_files"] == 5
    assert metrics["timing_ms"]["copy_pipeline"] > 0
    assert metrics["timing_ms"]["delete_stage"] > 0
    assert metrics["spans"]["post_delete_revalidation"]["succeeded"] == 1
    deep = data["stages"]["post_deep_verify"]["metrics"]
    assert deep["scopes"]["source_sha256"]["hash_bytes"] == deep["scopes"]["target_sha256"]["hash_bytes"]
    with pytest.raises(profile.Blocked, match="ALREADY_CALLED"):
        service.write_gate(SimpleNamespace())


@pytest.mark.parametrize("failure", ("instance_busy", "recovery", "low_space", "missing_parent", "source_missing", "empty_source", "device_changed", "readonly_device", "same_device", "alias"))
def test_fail_closed_safety_gates(service, monkeypatch, failure):
    put(service.source, "note.md")
    if failure == "instance_busy":
        monkeypatch.setattr(profile, "acquire_instance_lock", lambda _: None)
    elif failure == "recovery":
        monkeypatch.setattr(profile, "require_recovery_clear", lambda _: (_ for _ in ()).throw(SyncError("pending recovery")))
    elif failure == "low_space":
        monkeypatch.setattr(profile.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    elif failure == "missing_parent":
        service.target.parent.rmdir()
    elif failure == "source_missing":
        (service.source / "note.md").unlink()
        service.source.rmdir()
    elif failure == "empty_source":
        (service.source / "note.md").unlink()
        put(service.target, "keep.md")
    elif failure in {"readonly_device", "same_device"}:
        devices = profile.probe_devices()
        devices["H"]["read_only"] = failure == "readonly_device"
        if failure == "same_device":
            devices["H"]["disk"] = devices["E"]["disk"]
        monkeypatch.setattr(profile, "probe_devices", lambda: devices)
    elif failure == "alias":
        real_validate = profile.validate_roots

        def different(*args):
            context = real_validate(*args)
            context["target"] = str(service.target.parent / "other")
            return context
        monkeypatch.setattr(profile, "validate_roots", different)
    else:
        original = profile.probe_devices

        def changed():
            result = original()
            monkeypatch.setattr(paths, "volume_identity", lambda _: "changed-volume")
            return result
        monkeypatch.setattr(profile, "probe_devices", changed)
    before_source, before_target = manifest(service.source), manifest(service.target)
    monkeypatch.setattr(service.engine, "execute", lambda *_a, **_k: pytest.fail("gate bypass"))
    with ExitStack() as resources, pytest.raises((SyncError, OSError)):
        service.run(resources)
    assert manifest(service.source) == before_source and manifest(service.target) == before_target
    assert not service.report.data["execution_performed"]


@pytest.mark.parametrize("kind", ("equal", "source_contains", "target_contains", "reverse"))
def test_bad_endpoint_pairs_are_rejected(tmp_path, kind):
    a = tmp_path / "a"
    a.mkdir()
    b = a / "child"
    b.mkdir()
    if kind == "reverse":
        with pytest.raises(profile.Blocked, match="DIRECTION"):
            profile.require_direction(Path("H:/source"), Path("E:/target"))
    else:
        pairs = {"equal": (a, a), "source_contains": (a, b), "target_contains": (b, a)}
        with pytest.raises(SyncError):
            paths.validate_roots(*map(str, pairs[kind]))


def test_incomplete_analysis_has_private_error_and_cannot_execute(service, monkeypatch):
    put(service.source, "file.md")
    original = engine_module._scan

    def failed(root, *args, **kwargs):
        if root == str(service.source):
            raise PermissionError("private note path")
        return original(root, *args, **kwargs)
    monkeypatch.setattr(engine_module, "_scan", failed)
    with ExitStack() as resources, pytest.raises(profile.Blocked, match="INCOMPLETE"):
        service.run(resources)
    assert not service.target.exists()
    assert service.report.data["stages"]["analysis"]["status"] == "FAILED"


@pytest.mark.parametrize("failure", ("copy_error", "cancel"))
def test_execute_failure_stops_deletions_no_retry_and_cleans_stage(service, monkeypatch, failure):
    put(service.source, "new.md", b"a" * (8 * 1024**2))
    survivor = put(service.target, "private-survivor.md", b"keep")
    original = engine_module.copy_stream_and_hash

    def failing(*args, **kwargs):
        if failure == "copy_error":
            raise OSError("private failure")
        service.cancel.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(engine_module, "copy_stream_and_hash", failing)
    auto_operator(service, monkeypatch)
    with ExitStack() as resources:
        service.run(resources)
    assert survivor.read_bytes() == b"keep"
    assert service.report.data["result"]["engine_status"] == ("FAILED" if failure == "copy_error" else "CANCELLED")
    assert service.report.data["result"]["deleted_files"] == 0
    assert not list(service.target.rglob(".obmanage-*.tmp"))
    assert service.report.data["stages"]["execute"]["metrics"]["spans"]["source_to_stage"]["succeeded"] == 0


def test_reports_parse_and_shared_outputs_contain_no_private_names(service, monkeypatch):
    put(service.source, "private-note.md", b"private body SENTINEL")
    service.execute_requested = False
    with ExitStack() as resources, pytest.raises(profile.Blocked):
        service.run(resources)
    service.report.save()
    for name in ("run_metrics.json", "timeline.jsonl", "hotspots.csv", "scan_mechanism.md", "analysis_report.md", "chatgpt_summary.md"):
        text = (service.report.work / name).read_text(encoding="utf-8")
        for token in (str(service.source), str(service.target), str(service.state), "private-note", "SENTINEL", "mock-source", "12345678"):
            assert token not in text
    metrics = json.loads((service.report.work / "run_metrics.json").read_text(encoding="utf-8"))
    assert metrics["stages"]["post_deep_verify"]["duration_ms"] is None
    assert metrics["stages"]["post_deep_verify"]["status"] == "NOT_RUN"
    assert metrics["throughput"]["copy_phase_MiB_s"] is None
    assert list(csv.DictReader((service.report.work / "hotspots.csv").open(encoding="utf-8")))
    assert all(json.loads(line)["offset_ms"] >= 0 for line in (service.report.work / "timeline.jsonl").read_text().splitlines())
    assert "private-note" in (service.report.work / "private_plan.json").read_text(encoding="utf-8")


def test_detail_scope_metrics_survive_exception_cancel_and_parallel_workers():
    metrics = ScanMetrics("parallel", detail_phases=frozenset(("outer", "inner", "bad", "cancel")))

    def work():
        with phase("inner"):
            for _ in range(100):
                count("bytes", 1)
    with metrics.activate(), phase("outer"), ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(copy_context().run, work) for _ in range(2)]
        for job in jobs:
            job.result()
        with pytest.raises(OSError), phase("bad"):
            raise OSError("injected")
        with pytest.raises(SyncCancelled), phase("cancel"):
            raise SyncCancelled("injected")
    data = metrics.report()
    assert data["counts"]["bytes"] == data["scopes"]["outer"]["bytes"] == data["scopes"]["inner"]["bytes"] == 200
    assert data["spans"]["inner"]["calls"] == 2
    assert data["spans"]["bad"]["failed"] == data["spans"]["cancel"]["cancelled"] == 1
    assert all(span["last_offset_ns"] >= span["first_offset_ns"] >= 0 for span in data["spans"].values())
    assert current() is None


def test_report_output_inside_repository_is_rejected(service, monkeypatch):
    monkeypatch.setattr(profile, "ROOT", service.source)
    with pytest.raises(SyncError):
        profile.make_output(service.source, service.target)
    assert not (service.source / "artifacts").exists()


def test_report_refuses_overwritten_owned_file(service):
    service.report.save()
    path = service.report.work / "run_metrics.json"
    path.unlink()
    path.write_text("external replacement", encoding="utf-8")
    with pytest.raises(profile.Blocked, match="REPORT_REPLACED"):
        service.report.save()
    assert path.read_text(encoding="utf-8") == "external replacement"


def test_real_qt_instance_lock_and_current_getlockinfo_contract(service, monkeypatch):
    from obmanage.app import acquire_instance_lock
    monkeypatch.setattr(profile, "acquire_instance_lock", acquire_instance_lock)
    with ExitStack() as resources:
        service.precheck(resources)
        service.check()
        assert acquire_instance_lock(service.state) is None
    lock = acquire_instance_lock(service.state)
    assert lock is not None
    lock.unlock()


@pytest.mark.parametrize("confirm", (False, True))
def test_explicit_parent_intent_only_creates_after_local_confirmation(service, monkeypatch, confirm):
    put(service.source, "note.md", b"new source")
    service.target.parent.rmdir()
    service.create_target_parent = True
    before = manifest(service.source)
    if confirm:
        auto_operator(service, monkeypatch)
    else:
        operator(monkeypatch, ["no"])
    with ExitStack() as resources:
        if confirm:
            service.run(resources)
            assert service.report.data["status"] == "SUCCESS_VERIFIED"
            assert service.report.data["target_parent_created"]
            assert (service.target / "note.md").read_bytes() == b"new source"
        else:
            with pytest.raises(SyncCancelled):
                service.run(resources)
            assert not service.target.parent.exists()
            assert not service.execute_called
    assert manifest(service.source) == before


def test_newly_appeared_parent_is_not_silently_adopted(service):
    put(service.source, "note.md")
    service.target.parent.rmdir()
    service.create_target_parent = True
    with ExitStack() as resources:
        preview = prepared(service, resources)
        put(service.target.parent, "foreign.md", b"external")
        with pytest.raises(profile.Blocked, match="PARENT_APPEARED"):
            service.confirm(preview)
        assert (service.target.parent / "foreign.md").read_bytes() == b"external"
        assert not service.target.exists()


def test_parent_intent_in_dry_run_is_always_read_only(service, monkeypatch):
    put(service.source, "note.md")
    service.target.parent.rmdir()
    service.create_target_parent = True
    service.execute_requested = False
    forbid_writes(monkeypatch, service.source, service.target.parent)
    with ExitStack() as resources, pytest.raises(profile.Blocked, match="DRY_RUN"):
        service.run(resources)
    assert not service.target.parent.exists()


def test_console_wait_expires_or_cancels_without_keyboard_or_target_io(service):
    with pytest.raises(profile.Blocked, match="EXPIRED"):
        profile.read_console_phrase("test", 0, service.cancel)
    service.cancel.set()
    with pytest.raises(SyncCancelled):
        profile.read_console_phrase("test", profile.time.perf_counter_ns() + 10**9, service.cancel)


def test_console_reader_uses_only_local_keys_and_supports_backspace(service, monkeypatch):
    import msvcrt
    keys = iter("A\bB\r")
    monkeypatch.setattr(msvcrt, "kbhit", lambda: True)
    monkeypatch.setattr(msvcrt, "getwch", lambda: next(keys))
    assert profile.read_console_phrase("test", profile.time.perf_counter_ns() + 10**9, service.cancel) == "B"
