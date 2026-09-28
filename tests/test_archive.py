import os
import zipfile
from pathlib import Path
from threading import Event

import pytest

from obmanage.management import archive as module
from obmanage.management.archive import ArchiveEngine
from obmanage.models import SyncCancelled, SyncError


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "知识仓库"
    (root / ".obsidian").mkdir(parents=True)
    (root / "空目录").mkdir()
    (root / ".obsidian" / "app.json").write_bytes(b"{}")
    (root / "笔记.md").write_bytes("中文笔记".encode() * 1000)
    (root / "movie.MP4").write_bytes(b"video" * 100)
    (root / "code.ts").write_bytes(b"const x = 1;")
    return root


@pytest.mark.parametrize("level", [0, 1, 6, 9])
@pytest.mark.parametrize("exclude", [False, True])
def test_roundtrip_readonly_no_wrapper_and_empty_directories(tmp_path, vault, level, exclude):
    state = tmp_path / "state"
    out = tmp_path / "output"
    out.mkdir()
    before = {p.relative_to(vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
              for p in vault.rglob("*") if p.is_file()}
    engine = ArchiveEngine(state)
    plan = engine.analyze(str(vault), str(out), level=level, exclude_videos=exclude)
    assert not list(out.iterdir()) and not state.exists()  # preview is zero-write
    result = engine.execute(plan)
    with zipfile.ZipFile(result.output) as archive:
        assert archive.testzip() is None
        assert "空目录/" in archive.namelist()
        assert ".obsidian/app.json" in archive.namelist()
        assert ("movie.MP4" in archive.namelist()) is not exclude
        for name, (content, _, _) in before.items():
            if not exclude or name != "movie.MP4":
                assert archive.read(name) == content
                assert archive.getinfo(name).compress_type == (zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED)
    after = {p.relative_to(vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
             for p in vault.rglob("*") if p.is_file()}
    assert before == after
    assert len(list(out.iterdir())) == 1


def test_collection_and_parent_destination_allowed(tmp_path, vault):
    collection = tmp_path / "collection"
    collection.mkdir()
    vault.rename(collection / vault.name)
    engine = ArchiveEngine(tmp_path / "state")
    plan = engine.analyze(str(collection), str(tmp_path), filename="collection.zip")
    engine.execute(plan)
    with zipfile.ZipFile(plan.output) as archive:
        assert "知识仓库/笔记.md" in archive.namelist()


@pytest.mark.parametrize("change", ["edit", "new", "remove", "same_metadata"])
def test_stale_preview_never_publishes(tmp_path, vault, change):
    out = tmp_path / "output"
    out.mkdir()
    engine = ArchiveEngine(tmp_path / "state")
    plan = engine.analyze(str(vault), str(out))
    note = vault / "笔记.md"
    if change == "edit":
        note.write_bytes(b"changed")
    elif change == "new":
        (vault / "new.md").write_bytes(b"new")
    elif change == "remove":
        note.unlink()
    else:
        before = note.stat()
        note.write_bytes(b"x" * before.st_size)
        os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(SyncError):
        engine.execute(plan)
    assert not list(out.iterdir())


def test_video_filter_and_reserved_recovery_directory_visible(tmp_path, vault):
    recovery = vault / ".obmanage-deploy-private"
    recovery.mkdir()
    (recovery / "secret").write_bytes(b"must not enter zip")
    out = tmp_path / "out"
    out.mkdir()
    engine = ArchiveEngine(tmp_path / "state")
    plan = engine.analyze(str(vault), str(out), exclude_videos=True)
    assert {e.excluded for e in plan.entries if e.excluded} == {"视频", "事务恢复目录"}
    result = engine.execute(plan)
    with zipfile.ZipFile(result.output) as archive:
        assert not any(".obmanage-deploy" in n for n in archive.namelist())


@pytest.mark.parametrize("phase", ["archive", "verify", "source_verify"])
def test_cancel_removes_only_own_partial(tmp_path, vault, phase):
    out = tmp_path / "output"
    out.mkdir()
    unrelated = out / "other.partial"
    unrelated.write_bytes(b"keep")
    engine = ArchiveEngine(tmp_path / "state")
    plan = engine.analyze(str(vault), str(out))
    cancel = Event()
    def progress(event):
        if event.phase == phase:
            cancel.set()
    with pytest.raises(SyncCancelled):
        engine.execute(plan, cancel=cancel, progress=progress)
    assert list(out.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("failure", ["corrupt", "disk_full", "source_change", "collision", "volume"])
def test_failures_do_not_publish_or_overwrite(tmp_path, vault, monkeypatch, failure):
    out = tmp_path / "out"
    out.mkdir()
    engine = ArchiveEngine(tmp_path / "state")
    plan = engine.analyze(str(vault), str(out))
    verify = module._verify_zip
    def injected(path, entries, cancel, progress):
        if failure == "corrupt":
            Path(path).write_bytes(b"bad zip")
        elif failure == "disk_full":
            raise OSError("disk full")
        elif failure == "source_change":
            (vault / "late.md").write_bytes(b"late")
        elif failure == "collision":
            Path(plan.output).write_bytes(b"existing archive")
        elif failure == "volume":
            monkeypatch.setattr(module, "volume_identity", lambda _: "changed")
        return verify(path, entries, cancel, progress)
    monkeypatch.setattr(module, "_verify_zip", injected)
    with pytest.raises((SyncError, OSError, zipfile.BadZipFile)):
        engine.execute(plan)
    assert not list(out.glob("*.partial"))
    if failure == "collision":
        assert Path(plan.output).read_bytes() == b"existing archive"
    else:
        assert not Path(plan.output).exists()


def test_boundary_and_existing_archive(tmp_path, vault):
    engine = ArchiveEngine(tmp_path / "state")
    with pytest.raises(SyncError, match="来源"):
        engine.analyze(str(vault), str(vault))
    with pytest.raises(SyncError):
        ArchiveEngine(vault / "state").analyze(str(vault), str(tmp_path))
    with pytest.raises(SyncError):
        engine.analyze(str(vault), str(tmp_path), filename="../outside.zip")
    old = tmp_path / "old.zip"
    old.write_bytes(b"old")
    with pytest.raises(SyncError, match="同名"):
        engine.analyze(str(vault), str(tmp_path), filename="old.zip")
    assert old.read_bytes() == b"old"


def test_link_rejected(tmp_path, vault):
    try:
        (vault / "link").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(SyncError, match="链接"):
        ArchiveEngine(tmp_path / "state").analyze(str(vault), str(tmp_path))


def test_legacy_pending_recovery_blocks_archive(tmp_path, vault, seed_legacy_quarantine):
    (vault / ".trash").mkdir()
    (vault / ".trash" / "old.md").write_bytes(b"deleted")
    state = tmp_path / "state"
    seed_legacy_quarantine(state, vault)
    engine = ArchiveEngine(state)
    plan = engine.analyze(str(vault), str(tmp_path))
    with pytest.raises(SyncError, match="回收站"):
        engine.execute(plan)
    assert not Path(plan.output).exists()


def test_verified_zip_mutation_during_source_recheck_is_not_published(tmp_path, vault):
    out = tmp_path / "out"
    out.mkdir()
    engine = ArchiveEngine(tmp_path / "state")
    plan = engine.analyze(str(vault), str(out))
    def progress(event):
        if event.phase == "source_verify":
            partial = next(out.glob("*.partial"))
            with partial.open("ab") as stream:
                stream.write(b"modified")
    with pytest.raises(SyncError, match="校验后改变"):
        engine.execute(plan, progress=progress)
    assert not list(out.iterdir())


def test_read_scope_does_not_reuse_content_across_archive_phases(tmp_path, vault, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    engine = ArchiveEngine(tmp_path / "state")
    plan = engine.analyze(str(vault), str(out))
    original = module._verify_zip
    def mutate_after_zip_verification(*args, **kwargs):
        original(*args, **kwargs)
        note = vault / "笔记.md"
        before = note.stat()
        note.write_bytes(b"x" * before.st_size)
        os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(module, "_verify_zip", mutate_after_zip_verification)
    with pytest.raises(SyncError, match="来源"):
        engine.execute(plan)
    assert not list(out.iterdir())


def test_preview_final_inventory_rejects_file_created_during_hash(tmp_path, vault):
    engine = ArchiveEngine(tmp_path / "state")
    inserted = False
    def progress(event):
        nonlocal inserted
        if event.phase == "scan" and event.relative_path and not inserted:
            inserted = True
            (vault / "late.md").write_bytes(b"must not be silently omitted")
    with pytest.raises(SyncError, match="清单"):
        engine.analyze(str(vault), str(tmp_path), progress=progress)
    assert inserted
    assert not list(tmp_path.glob("*.zip"))


def test_empty_vault_old_timestamps_long_paths_and_cancelled_preview(tmp_path, vault):
    note = vault / "笔记.md"
    os.utime(note, (0, 0))
    deep = vault.joinpath(*(["很长的目录" * 3] * 14))
    os.makedirs(module.native(deep))
    with open(module.native(deep / "深层.md"), "wb") as stream:
        stream.write(b"long path")
    engine = ArchiveEngine(tmp_path / "state")
    cancel = Event()
    cancel.set()
    with pytest.raises(SyncCancelled):
        engine.analyze(str(vault), str(tmp_path), cancel=cancel)
    plan = engine.analyze(str(vault), str(tmp_path))
    result = engine.execute(plan)
    with zipfile.ZipFile(result.output) as archive:
        assert archive.getinfo("笔记.md").date_time[0] == 1980
        assert any(archive.read(n) == b"long path" for n in archive.namelist() if n.endswith("深层.md"))
    empty = tmp_path / "empty"
    (empty / ".obsidian").mkdir(parents=True)
    result = engine.execute(engine.analyze(str(empty), str(tmp_path)))
    with zipfile.ZipFile(result.output) as archive:
        assert archive.namelist() == [".obsidian/"]


def test_baidu_launch_requires_configured_client_and_uses_literal_path(tmp_path, monkeypatch):
    from obmanage.management import desktop_actions
    if os.name != "nt":
        pytest.skip("Windows integration")
    called = []
    monkeypatch.setattr(desktop_actions.os, "startfile", called.append)
    with pytest.raises(ValueError):
        desktop_actions.open_baidu(str(tmp_path / "missing.exe"))
    client = tmp_path / "BaiduNetdisk.exe"
    client.write_bytes(b"mock client, never executed")
    assert "已请求" in desktop_actions.open_baidu(str(client))
    assert called == [str(client.resolve())]
