"""The experiment's only writable fixtures are isolated on the system volume."""
import os
import shutil
from contextlib import ExitStack
from pathlib import Path
from threading import Event

import pytest

from obmanage.engine import SyncEngine
from obmanage.management.incremental import selected_plans
from obmanage.models import PlanItem, SyncError
from obmanage.paths import hold_windows_directory
from tools import experiment_initial_sync as experiment
from tools.benchmark_readonly_paths import BoundaryError, Protection, system_temp


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows creation semantics")


@pytest.fixture
def setup(tmp_path):
    assert tmp_path.drive == system_temp().drive
    local, parent, work, recovery = (tmp_path / name for name in ("local", "parent", "work", "recovery"))
    for directory in (local, parent, work, work / "empty-preview", recovery):
        directory.mkdir()
    target = parent / "target"
    data = {"target_original": "missing"}
    guard = Protection(local, parent, work)
    service = experiment.InitialSync(local, target, work, recovery, guard, Event(), data, lambda _: None)
    yield service


def vault(root, name, **files):
    path = root / name
    (path / ".obsidian").mkdir(parents=True)
    for relative, content in files.items():
        child = path / relative
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_bytes(content)
    return path


def create_root(service, pins):
    pins.enter_context(service.protection.inputs["portable"].hold())
    preview = service.preview("CREATE")
    assert not service.target.exists()
    service.create_root(preview, preview.phrase, pins)


def copy(service, kind="CANARY"):
    preview = service.preview(kind)
    service.run_batch(preview, preview.phrase)


def test_missing_root_canary_nested_coverage_hashes_rescan(setup):
    service = setup
    small = vault(service.source, "small", **{"a.md": b"note"})
    big = vault(service.source, "group/big", **{
        ".obsidian/plugin.json": b"plugin", ".trash/old.md": b"trashed", "video.mp4": b"movie",
        "attachments/image.png": b"image",
    })
    (big / "empty").mkdir()
    vault(big, "nested", **{"nested.md": b"nested note"})
    (service.source / "orphan").mkdir()
    (service.source / "outside.md").write_bytes(b"outside")
    assert small.is_dir()
    with ExitStack() as pins:
        create_root(service, pins)
        copy(service)
        assert {p.name for p in service.target.iterdir()} == {"small"}
        copy(service, "COPY")
        service.rescan()
    data = service.report
    assert (data["discovered"], data["top_level"], data["nested"]) == (3, 2, 1)
    assert data["coverage_gap"] == dict(files=1, directories=1, bytes=7, largest_file=7)
    assert data["rescan"] == {"identical": 3, "changed": 0}
    assert data["audits"][-1]["source_bytes"] == data["audits"][-1]["target_bytes"] == 38
    assert (service.target / "group/big/empty").is_dir()
    assert (service.target / "group/big/nested/nested.md").read_bytes() == b"nested note"
    assert not (service.target / "outside.md").exists()
    for row in data["vaults"]:
        counts = row["counts"]
        assert row["deleted"] == 0
        assert (row["copied_bytes"] == counts["source_read_bytes"] == counts["stage_write_bytes"]
                == counts["staged_sha256_hash_bytes"])
    assert data["scans"][-1]["counts"]["baseline_hits"] > 0


@pytest.mark.parametrize("name", [".hidden", "foreign.md", "empty_dir", "unmarked_vault"])
def test_nonempty_target_is_never_adopted(setup, name):
    service = setup
    vault(service.source, "unmarked_vault", **{"a": b"source"})
    service.target.mkdir()
    service.missing = False
    service.target_binding = experiment.identity(experiment.snapshot(service.target))
    existing = service.target / name
    if name in {"empty_dir", "unmarked_vault"}:
        existing.mkdir()
    else:
        existing.write_bytes(b"unknown")
    with pytest.raises(experiment.ExperimentError, match="非空"):
        service.preview("CANARY")
    assert list(service.target.iterdir()) == [existing]


@pytest.mark.parametrize("action", ["update", "delete", "rename", "rmdir", "exclude", "unknown"])
def test_mutated_plan_refuses_every_other_action(setup, action):
    service = setup
    vault(service.source, "a", **{"note.md": b"payload"})
    analysis = service.preview("CREATE").analysis
    plan = analysis.pairs[0].local_plan
    plan.items.append(PlanItem(action, "intruder"))
    with pytest.raises(SyncError):
        SyncEngine.require_new_target(plan)
    assert not service.target.exists()


def test_reverse_and_parent_child_overlap_are_rejected(setup):
    service = setup
    vault(service.source, "a", **{"note.md": b"source"})
    vault(service.source, "a/nested", **{"child.md": b"nested"})
    analysis = service.preview("CREATE").analysis
    with pytest.raises(SyncError, match="重叠"):
        selected_plans(analysis, {"a": "local", "a/nested": "local"})
    surrogate = service.work / "empty-preview"
    vault(surrogate, "extra", **{"b": b"reverse"})
    reverse = service.engine.analyze(str(service.source), str(surrogate))
    result = service.engine.execute(reverse, {"extra": "portable"}, create_only=True)
    assert result.status == "failed"
    assert not (service.source / "extra").exists()


@pytest.mark.parametrize("mutation", ["modified", "removed", "cancelled", "target_arrived", "bad_phrase", "expired"])
def test_write_gate_rejects_stale_or_unconfirmed_preview(setup, monkeypatch, mutation):
    service = setup
    note = vault(service.source, "a", **{"note": b"source"}) / "note"
    preview = service.preview("CREATE")
    response = preview.phrase
    if mutation == "modified":
        note.write_bytes(b"new content")
    elif mutation == "removed":
        note.unlink()
    elif mutation == "cancelled":
        service.cancel.set()
    elif mutation == "target_arrived":
        service.target.mkdir()
        (service.target / "foreign").write_bytes(b"keep")
    elif mutation == "bad_phrase":
        response = "yes"
    else:
        monkeypatch.setattr(experiment, "MAX_PREVIEW_AGE", -1)
    with ExitStack() as pins, pytest.raises((SyncError, experiment.SyncCancelled)):
        service.create_root(preview, response, pins)
    assert service.target.exists() == (mutation == "target_arrived")
    if service.target.exists():
        assert (service.target / "foreign").read_bytes() == b"keep"


def test_same_name_after_final_check_is_not_overwritten(setup, monkeypatch):
    from obmanage import engine

    service = setup
    vault(service.source, "a", **{"note.md": b"source"})
    original = engine.publish_windows_stage
    added = []

    def late_collision(handle, dst):
        if Path(dst).name == "note.md":
            Path(dst).write_bytes(b"concurrent owner")
            added.append(dst)
        return original(handle, dst)

    with ExitStack() as pins:
        create_root(service, pins)
        monkeypatch.setattr(engine, "publish_windows_stage", late_collision)
        with pytest.raises(experiment.ExperimentError, match="原生执行停止"):
            copy(service)
    assert len(added) == 1
    assert (service.target / "a/note.md").read_bytes() == b"concurrent owner"
    assert not list(service.target.rglob(".obmanage-*.tmp"))
    assert service.report["vaults"][0]["copied_files"] == 0


@pytest.mark.parametrize("failure", ["no_space", "unreadable", "volume", "recovery", "cancel", "bad_hash"])
def test_failed_vault_stops_batch_and_preserves_published_content(setup, monkeypatch, failure):
    service = setup
    for name in ("a", "b", "c"):
        vault(service.source, name, **{"note.md": name.encode()})
    with ExitStack() as pins:
        create_root(service, pins)
        copy(service)
        remaining = service.preview("COPY")
        if failure == "no_space":
            usage = shutil.disk_usage(service.work)
            monkeypatch.setattr(shutil, "disk_usage", lambda _: usage._replace(free=0))
        elif failure == "unreadable":
            original = os.scandir

            def scandir(path):
                if Path(path).name == "b":
                    raise PermissionError("fixture")
                return original(path)

            monkeypatch.setattr(os, "scandir", scandir)
        elif failure == "volume":
            from tools import benchmark_readonly_paths as paths
            original = paths.volume_identity

            def moved(path):
                return "different volume" if Path(path).name == "parent" else original(path)

            monkeypatch.setattr(paths, "volume_identity", moved)
        elif failure == "recovery":
            def pending(_):
                raise SyncError("fixture recovery pending")

            monkeypatch.setattr(experiment, "require_recovery_clear", pending)
        elif failure == "cancel":
            original = SyncEngine._write_copy_temp

            def cancel_copy(pending, event):
                service.cancel.set()
                return original(pending, event)

            monkeypatch.setattr(SyncEngine, "_write_copy_temp", staticmethod(cancel_copy))
        else:
            from obmanage import engine
            original = engine._hash_file

            def corrupt(path, *args, **kwargs):
                return "incorrect" if str(path).endswith(".tmp") else original(path, *args, **kwargs)

            monkeypatch.setattr(engine, "_hash_file", corrupt)
        with pytest.raises((experiment.ExperimentError, SyncError)):
            service.run_batch(remaining, remaining.phrase)
    assert (service.target / "a/note.md").read_bytes() == b"a"
    assert not (service.target / "b/note.md").exists()
    assert not (service.target / "c").exists()
    assert not list(service.target.rglob(".obmanage-*.tmp"))


@pytest.mark.parametrize("drift", ["unknown", "same_size_content", "mtime_only"])
def test_postcopy_independent_audit_rejects_unknown_or_corrupted_content(setup, drift):
    service = setup
    vault(service.source, "a", **{"note": b"source"})
    with ExitStack() as pins:
        create_root(service, pins)
        copy(service)
        note = service.target / "a/note"
        saved = note.stat()
        if drift == "unknown":
            (service.target / "other").write_bytes(b"extra")
        elif drift == "same_size_content":
            note.write_bytes(b"change")
            os.utime(note, ns=(saved.st_atime_ns, saved.st_mtime_ns))
        else:
            os.utime(note, ns=(saved.st_atime_ns, saved.st_mtime_ns - 2_000_000_000))
        expected = experiment.projection(service.source_entries, service.completed)
        if drift == "mtime_only":
            experiment.audit(service.source, service.target, expected, service.cancel, service.report)
            assert service.report["audits"][-1]["metadata"] == {"mtime_differs": 1}
        else:
            with pytest.raises(experiment.ExperimentError) as raised:
                experiment.audit(service.source, service.target, expected, service.cancel, service.report)
            assert raised.value.status == "MISMATCH"
        with pytest.raises(experiment.ExperimentError):
            service.preview("COPY")


def test_symlink_and_reserved_directory_are_not_followed(setup):
    service = setup
    directory = vault(service.source, "a")
    (directory / "alias").symlink_to(service.work, target_is_directory=True)
    with pytest.raises(experiment.ExperimentError, match="链接"):
        service.preview("CREATE")
    (directory / "alias").rmdir()
    (directory / ".obmanage-deploy-fixture").mkdir()
    with pytest.raises(experiment.ExperimentError, match="恢复目录"):
        service.preview("CREATE")
    assert not service.target.exists()


def test_directory_create_is_exclusive_and_pins_the_new_identity(setup):
    service = setup
    with hold_windows_directory(service.target, create=True):
        with pytest.raises(FileExistsError):
            with hold_windows_directory(service.target, create=True):
                pytest.fail("existing target was adopted")
        with pytest.raises(PermissionError):
            service.target.rename(service.target.with_name("moved"))
    assert service.target.exists()


def test_state_locations_cannot_intersect_the_live_inputs(setup):
    service = setup
    with pytest.raises(BoundaryError):
        Protection(service.source, service.target.parent, service.target.parent)


def test_lost_application_lock_refuses_even_a_confirmed_root_creation(setup):
    class LostLock:
        def getLockInfo(self):
            return True, os.getpid() + 1, "", ""

    service = setup
    vault(service.source, "a", **{"note": b"source"})
    preview = service.preview("CREATE")
    service.application_lock = LostLock()
    with ExitStack() as pins, pytest.raises(experiment.ExperimentError, match="任务锁"):
        service.create_root(preview, preview.phrase, pins)
    assert not service.target.exists()
