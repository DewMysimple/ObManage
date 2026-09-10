"""Workflow safety: the selected origin is read-only in either direction."""
from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import obmanage.engine as engine_module
import obmanage.paths as paths_module
from obmanage.engine import SyncEngine
from obmanage.paths import canonical, snapshot, validate_roots
from obmanage.store import BaselineStore


@pytest.fixture
def workstations(tmp_path):
    computer, portable = tmp_path / "主机仓库", tmp_path / "移动硬盘仓库"
    computer.mkdir()
    portable.mkdir()
    return SyncEngine(tmp_path / "state"), computer, portable


def put(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def contents(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def immutable_snapshot(root: Path) -> dict:
    """Access times can change naturally on reads; content/mtime/identity cannot."""
    return {p.relative_to(root).as_posix(): (snapshot(p), p.read_bytes() if p.is_file() else None)
            for p in [root, *root.rglob("*")]}


def sync(engine: SyncEngine, source: Path, target: Path):
    plan = engine.analyze(str(source), str(target))
    assert plan.can_execute, plan.errors
    result = engine.execute(plan)
    assert result.status == "success", result.errors
    return plan, result


def forbid_mutations(monkeypatch, root: Path) -> list:
    """Fail on even a transient source write; a final content comparison alone
    would miss writing then restoring the old bytes or changing a timestamp.
    The engine's fdopen writes belong to mkstemp-created files, whose directory
    is checked separately below.
    """
    protected = os.path.normcase(canonical(root))
    mutations = []

    def check(path):
        if isinstance(path, int):
            return
        resolved = os.path.normcase(canonical(path))
        try:
            inside = os.path.commonpath((protected, resolved)) == protected
        except ValueError:
            inside = False
        assert not inside, f"Attempted mutation of the selected origin: {path}"
        mutations.append(resolved)

    def wrap_mutator(original, count):
        def wrapped(*args, **kwargs):
            for path in args[:count]:
                check(path)
            return original(*args, **kwargs)
        return wrapped

    for name, count in [("unlink", 1), ("remove", 1), ("rmdir", 1), ("mkdir", 1),
                        ("makedirs", 1), ("rename", 2), ("replace", 2), ("utime", 1), ("chmod", 1)]:
        monkeypatch.setattr(os, name, wrap_mutator(getattr(os, name), count))

    def wrap_open(original):
        def wrapped(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                check(file)
            return original(file, mode, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(builtins, "open", wrap_open(builtins.open))
    monkeypatch.setattr(io, "open", wrap_open(io.open))
    original_mkstemp = engine_module.tempfile.mkstemp

    def checked_mkstemp(*args, **kwargs):
        check(kwargs["dir"])
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(engine_module.tempfile, "mkstemp", checked_mkstemp)
    return mutations


def test_computer_portable_computer_round_trip_mirrors_laptop_edits(workstations, monkeypatch):
    engine, computer, portable = workstations
    put(computer, "笔记/日记.md", "主机上的旧版本".encode())
    put(computer, "旧目录/已删除.md", b"delete on laptop")
    put(computer, ".obsidian/app.json", b'{"theme":"dark"}')
    put(computer, "视频/课程.mp4", b"immutable video" * 4096)
    put(computer, "旧名字.md", b"rename on laptop")
    (computer / "空目录").mkdir()
    _, outward = sync(engine, computer, portable)
    assert outward.copied_files == 5

    put(portable, "笔记/日记.md", "笔记本上的新版本".encode())
    put(portable, ".obsidian/app.json", b'{"theme":"light"}')
    put(portable, "笔记/笔记本新增.md", "在笔记本工作时新增".encode())
    (portable / "旧目录/已删除.md").unlink()
    (portable / "旧目录").rmdir()
    (portable / "旧名字.md").rename(portable / "新名字.md")
    (portable / "空目录").rmdir()
    put(computer, "仅主机有.md", b"removed by portable authority")
    origin_before = immutable_snapshot(portable)
    computer_before = immutable_snapshot(computer)
    video_identity = (computer / "视频/课程.mp4").stat().st_ino
    mutations = forbid_mutations(monkeypatch, portable)

    plan = engine.analyze(str(portable), str(computer))

    assert plan.can_execute, plan.errors
    assert immutable_snapshot(portable) == origin_before
    assert immutable_snapshot(computer) == computer_before, "Analysis must not change either vault"
    assert plan.counts["update"] == 2
    assert plan.counts["add"] == 2
    assert plan.counts["delete"] == 3
    assert plan.counts["rmdir"] == 2
    assert plan.counts["skip"] == 1
    result = engine.execute(plan)

    assert result.status == "success", result.errors
    assert mutations, "The write guard must be exercised by real target changes"
    assert immutable_snapshot(portable) == origin_before
    assert contents(computer) == contents(portable)
    assert (computer / "视频/课程.mp4").stat().st_ino == video_identity
    assert result.copied_files == 4
    assert result.deleted_files == 3
    assert result.deleted_dirs == 2


@pytest.mark.parametrize("source_side", ["computer", "portable"])
@pytest.mark.parametrize("failure", ["cancel", "disk_full", "replace_failure"])
def test_source_is_unchanged_even_when_sync_is_interrupted(workstations, monkeypatch, source_side, failure):
    engine, computer, portable = workstations
    source, target = (computer, portable) if source_side == "computer" else (portable, computer)
    put(source, "updated.md", b"new origin content")
    put(source, "unchanged.md", b"same")
    put(target, "updated.md", b"old target")
    put(target, "unchanged.md", b"same")
    put(target, "obsolete.md", b"keep on failure")
    original = immutable_snapshot(source)
    forbid_mutations(monkeypatch, source)
    plan = engine.analyze(str(source), str(target))
    assert plan.can_execute, plan.errors
    cancelled = Event()

    def progress(event):
        if failure == "cancel" and event.phase == "copy":
            cancelled.set()

    if failure == "disk_full":
        monkeypatch.setattr(engine_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    elif failure == "replace_failure":
        def locked(*args):
            raise PermissionError("target file is locked")
        monkeypatch.setattr(engine_module.os, "replace", locked)

    result = engine.execute(plan, cancel=cancelled, progress=progress)

    assert result.status in ("cancelled", "failed")
    assert result.errors
    assert immutable_snapshot(source) == original
    assert (target / "updated.md").read_bytes() == b"old target"
    assert (target / "obsolete.md").read_bytes() == b"keep on failure"


def test_empty_portable_requires_confirmation_and_only_clears_computer(workstations, monkeypatch):
    engine, computer, portable = workstations
    put(computer, "旧目录/旧笔记.md", b"only in computer")
    original = immutable_snapshot(portable)
    forbid_mutations(monkeypatch, portable)
    plan = engine.analyze(str(portable), str(computer))
    assert plan.source_empty
    assert engine.execute(plan).status == "failed"
    assert (computer / "旧目录/旧笔记.md").exists()

    result = engine.execute(plan, allow_empty=True)

    assert result.status == "success", result.errors
    assert contents(computer) == {}
    assert immutable_snapshot(portable) == original


def test_switching_directions_reuses_only_verified_equivalence(workstations, monkeypatch):
    engine, computer, portable = workstations
    put(computer, "电影.mp4", b"large stable payload" * 8192)
    put(portable, "电影.mp4", b"large stable payload" * 8192)
    # A common NTFS/exFAT scenario: matching bytes, unequal precision/time.
    os.utime(computer / "电影.mp4", ns=(1_700_000_000_123_456_700,) * 2)
    os.utime(portable / "电影.mp4", ns=(1_700_000_002_000_000_000,) * 2)
    outward = engine.analyze(str(computer), str(portable))
    assert outward.can_execute
    assert outward.counts["skip"] == 1

    def unnecessary_hash(*args, **kwargs):
        pytest.fail("Switching direction must reuse equivalent unchanged file versions")

    monkeypatch.setattr(engine_module, "_hash_file", unnecessary_hash)
    returning, result = sync(engine, portable, computer)
    assert returning.pair_id != outward.pair_id
    assert returning.context["reverse_pair_id"] == outward.pair_id
    assert "反向" in returning.items[0].reason
    assert result.copied_bytes == 0
    store = BaselineStore(engine.state_dir)
    try:
        forward_record = store.records(outward.pair_id)["电影.mp4"]
        reverse_record = store.records(returning.pair_id)["电影.mp4"]
        assert reverse_record == (forward_record[1], forward_record[0], forward_record[2])
    finally:
        store.close()


@pytest.mark.parametrize("changed_side", ["computer", "portable"])
def test_changed_file_invalidates_opposite_direction_baseline(workstations, monkeypatch, changed_side):
    engine, computer, portable = workstations
    put(computer, "note.md", b"original")
    sync(engine, computer, portable)
    changed = (computer if changed_side == "computer" else portable) / "note.md"
    changed.write_bytes(b"modified")
    previous = changed.stat()
    os.utime(changed, ns=(previous.st_atime_ns, previous.st_mtime_ns + 3_000_000_000))
    hashed = []
    real_hash = engine_module._hash_file

    def record_hash(path, *args, **kwargs):
        hashed.append(canonical(path))
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_hash_file", record_hash)
    plan = engine.analyze(str(portable), str(computer))

    assert plan.can_execute, plan.errors
    assert plan.counts["update"] == 1
    assert set(hashed) == {str(computer / "note.md"), str(portable / "note.md")}
    result = engine.execute(plan)
    assert result.status == "success", result.errors
    assert (computer / "note.md").read_bytes() == (portable / "note.md").read_bytes()


def test_fresh_reverse_record_can_supersede_stale_current_direction_record(workstations, monkeypatch):
    engine, computer, portable = workstations
    put(computer, "note.md", b"first version")
    sync(engine, computer, portable)
    put(portable, "note.md", b"edited on laptop")
    sync(engine, portable, computer)

    def unnecessary_hash(*args, **kwargs):
        pytest.fail("A newer verified reverse baseline supersedes the stale forward record")

    monkeypatch.setattr(engine_module, "_hash_file", unnecessary_hash)
    plan, result = sync(engine, computer, portable)
    assert plan.counts["skip"] == 1
    assert result.copied_bytes == 0


@pytest.mark.parametrize("change", ["path", "volume"])
def test_unrelated_path_or_replacement_volume_never_reuses_opposite_baseline(workstations, monkeypatch, change):
    engine, computer, portable = workstations
    put(computer, "note.md", b"same bytes")
    sync(engine, computer, portable)
    if change == "path":
        replacement = portable.with_name("不同的移动仓库")
        portable.rename(replacement)
        portable = replacement
    else:
        real_volume = paths_module.volume_identity
        monkeypatch.setattr(paths_module, "volume_identity", lambda p: real_volume(p) + ":replacement")
    hashed = []
    real_hash = engine_module._hash_file

    def record_hash(path, *args, **kwargs):
        hashed.append(path)
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_hash_file", record_hash)
    plan = engine.analyze(str(portable), str(computer))

    assert plan.can_execute, plan.errors
    assert plan.counts["skip"] == 1
    assert len(hashed) == 2, "Identical file metadata is insufficient across paths or volumes"


def test_full_verification_bypasses_both_direction_caches(workstations, monkeypatch):
    engine, computer, portable = workstations
    put(computer, "note.md", b"same bytes")
    sync(engine, computer, portable)
    sync(engine, portable, computer)
    hashed = []
    real_hash = engine_module._hash_file

    def record_hash(path, *args, **kwargs):
        hashed.append(path)
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_hash_file", record_hash)
    plan = engine.analyze(str(portable), str(computer), deep=True)
    assert plan.can_execute, plan.errors
    assert len(hashed) == 2


def test_pair_key_preserves_existing_database_compatibility(workstations):
    _, computer, portable = workstations
    context = validate_roots(str(computer), str(portable))
    legacy_key = hashlib.sha256(json.dumps(
        [os.path.normcase(str(computer)), os.path.normcase(str(portable)),
         context["source_volume"], context["target_volume"]],
        ensure_ascii=False).encode("utf-8")).hexdigest()
    assert context["pair_id"] == legacy_key
