"""Real filesystem and native notification checks, in isolated disposable repositories."""
import ctypes
import os
import struct
import threading
import time
from contextlib import closing

import pytest

from obmanage import realtime as live
from obmanage.engine import SYNC_MODE_MIRROR, SYNC_MODE_NO_VIDEO
from obmanage.models import SyncCancelled, SyncError
from obmanage.paths import identity, native, snapshot
from obmanage.realtime_store import LiveCopyRecords, RealtimeStore
from obmanage.realtime_watch import DirectoryWatch, changed_names


class Pair:
    def __init__(self, root, mode):
        self.src, self.dst, self.state = (root / n for n in ("电脑", "硬盘", "状态"))
        self.src.mkdir()
        self.dst.mkdir()
        self.mode = mode
        self.cancel = threading.Event()
        (self.src / "保留.md").write_text("keep")

    def enable(self, confirm=lambda p: True):
        self.token = live.enable_realtime(self.state, str(self.src), str(self.dst), self.mode,
                                          self.cancel, None, confirm)

    def run(self, *names):
        return live.reconcile(self.state, str(self.src), str(self.dst), self.mode, self.token,
                              names or None, self.cancel)


@pytest.fixture(params=[SYNC_MODE_MIRROR, SYNC_MODE_NO_VIDEO])
def pair(tmp_path, request):
    return Pair(tmp_path, request.param)


def test_confirm_first_diff_or_cancel_without_writes(pair):
    (pair.dst / "已有.md").write_text("old")
    seen = []
    with pytest.raises(SyncCancelled):
        pair.enable(lambda p: seen.append(p.counts) or False)
    assert seen[0]["delete"] == 1 and seen[0]["add"] == 1
    assert not (pair.dst / "保留.md").exists()
    assert (pair.dst / "已有.md").read_text() == "old"
    pair.enable()
    assert not (pair.dst / "已有.md").exists()
    assert pair.run().issues == {}


def test_single_save_skips_unrelated_files_and_directories(pair, monkeypatch):
    d = pair.src / "笔记"
    d.mkdir()
    (d / "a.md").write_bytes(b"old")
    (d / "other").write_bytes(b"other")
    pair.enable()
    (d / "a.md").write_bytes(b"one saved version")
    monkeypatch.setattr(live, "_scan", lambda *a, **kw: pytest.fail("walked unrelated directory"))
    observed = []
    digest = live._digest
    def record(root, name, state, cancel):
        observed.append(name)
        return digest(root, name, state, cancel)
    monkeypatch.setattr(live, "_digest", record)
    result = pair.run("笔记/a.md")
    assert result.issues == {}
    assert result.copies.copied_files == 1
    assert set(observed) == {"笔记/a.md"}
    assert (pair.dst / "笔记/a.md").read_bytes() == b"one saved version"


@pytest.mark.parametrize("foreign", ["edit", "delete", "add"])
def test_foreign_target_preserved_while_another_save_completes(pair, foreign):
    file = pair.src / "冲突.md"
    if foreign != "add":
        file.write_bytes(b"111")
    pair.enable()
    target = pair.dst / file.name
    file.write_bytes(b"222")
    if foreign == "delete":
        target.unlink()
    else:
        target.write_bytes(b"333")
    other = pair.src / "other.md"
    other.write_text("ok")
    out = pair.run()
    assert file.name in out.issues
    assert (pair.dst / other.name).read_text() == "ok"
    assert target.read_bytes() == b"333" if foreign != "delete" else not target.exists()
    assert file.read_bytes() == b"222"


def test_move_copies_new_before_delayed_removal_even_if_only_old_event(pair, monkeypatch):
    (pair.src / "old").mkdir()
    (pair.src / "old/n.md").write_bytes(b"note")
    pair.enable()
    (pair.src / "old").rename(pair.src / "new")
    now = time.time()
    monkeypatch.setattr(live.time, "time", lambda: now)
    first = pair.run("old")
    assert not first.issues and first.retry
    assert (pair.dst / "old/n.md").exists() and (pair.dst / "new/n.md").read_bytes() == b"note"
    now += 8
    second = pair.run(*first.retry)
    assert not second.issues and not second.retry and not (pair.dst / "old").exists()


def test_restored_file_restarts_delete_grace(pair, monkeypatch):
    item = pair.src / "old"
    item.write_text("x")
    pair.enable()
    now = time.time()
    monkeypatch.setattr(live.time, "time", lambda: now)
    item.unlink()
    assert pair.run("old").retry == {"old"}
    now += 20
    item.write_text("x")
    assert not pair.run("old").retry
    item.unlink()
    assert pair.run("old").retry == {"old"}
    assert (pair.dst / "old").exists()


def test_mass_and_empty_source_keep_every_target_with_names(pair):
    for i in range(16):
        (pair.src / str(i)).write_text("q")
    pair.enable()
    for i in range(10):
        (pair.src / str(i)).unlink()
    out = pair.run("0")
    assert "10 项" in out.issues[""] and all(str(i) in out.issues for i in range(10))
    for path in pair.src.iterdir():
        path.unlink()
    out = pair.run()
    assert "有效来源为空" in out.issues[""] and len(list(pair.dst.iterdir())) == 17


def test_late_write_and_copy_failure_hold_back_all_deletions(pair, monkeypatch):
    (pair.src / "old").write_text("delete later")
    pair.enable()
    (pair.src / "old").unlink()
    (pair.src / "保留.md").write_text("new")
    original = live.SyncEngine.copy_observed_file
    def late_write(*a, **kw):
        (pair.dst / "保留.md").write_text("on portable")
        return original(*a, **kw)
    monkeypatch.setattr(live.SyncEngine, "copy_observed_file", late_write)
    out = pair.run("old")
    assert out.issues
    assert (pair.dst / "保留.md").read_text() == "on portable"
    assert (pair.dst / "old").exists()
    assert not list(pair.dst.glob(".obmanage-*.tmp"))


def test_new_nested_parents_and_mode_protection(pair, monkeypatch):
    (pair.dst / "独有视频").mkdir()
    (pair.dst / "独有视频/movie.MP4").write_bytes(b"old video")
    pair.enable()
    new = pair.src / "folder/deep"
    new.mkdir(parents=True)
    (new / "a.jpg").write_bytes(b"image")
    (new / "movie.mp4").write_bytes(b"video")
    assert not pair.run("folder/deep/a.jpg").issues
    assert (pair.dst / "folder/deep/a.jpg").read_bytes() == b"image"
    assert (pair.dst / "folder/deep/movie.mp4").exists() == (pair.mode == SYNC_MODE_MIRROR)
    assert (pair.dst / "独有视频/movie.MP4").exists() == (pair.mode == SYNC_MODE_NO_VIDEO)


def test_replacement_root_and_recovery_gate_keep_target_untouched(pair, monkeypatch):
    pair.enable()
    (pair.src / "保留.md").write_text("changed")
    pair.dst.rename(pair.dst.with_name("original"))
    pair.dst.mkdir()
    with pytest.raises(SyncError, match="替换|改变"):
        pair.run()
    assert not list(pair.dst.iterdir())
    pair.dst.rmdir()
    pair.dst.with_name("original").rename(pair.dst)
    def gate(state):
        raise SyncError("待恢复事务")
    monkeypatch.setattr(live, "require_recovery_clear", gate)
    with pytest.raises(SyncError, match="待恢复"):
        pair.run()
    assert (pair.dst / "保留.md").read_text() == "keep"


def test_observed_case_rename_and_mode_namespace(pair):
    (pair.src / "lower.md").write_text("keep")
    pair.enable()
    (pair.src / "lower.md").rename(pair.src / "UPPER-TEMP")
    (pair.src / "UPPER-TEMP").rename(pair.src / "Lower.md")
    out = pair.run("lower.md", "Lower.md")
    assert not out.issues
    assert "Lower.md" in [p.name for p in pair.dst.iterdir()]
    with closing(RealtimeStore(pair.state)) as store, pytest.raises(SyncError):
        store.roots(pair.token, SYNC_MODE_NO_VIDEO if pair.mode == SYNC_MODE_MIRROR else SYNC_MODE_MIRROR)


def test_equal_size_and_restored_target_mtime_detects_foreign_content(pair):
    original = pair.src / "保留.md"
    pair.enable()
    dest = pair.dst / original.name
    st = dest.stat()
    dest.write_bytes(b"swap")
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    original.write_bytes(b"work")
    assert original.name in pair.run(original.name).issues
    assert dest.read_bytes() == b"swap"


def test_idle_reuses_exact_pair_and_explicit_check_rehashes(pair, monkeypatch):
    pair.enable()
    original = live._hash_file
    paths = []
    def read(path, *args, **kwargs):
        paths.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(live, "_hash_file", read)
    live.reconcile(pair.state, str(pair.src), str(pair.dst), pair.mode, pair.token,
                   None, pair.cancel, idle=True)
    assert not paths
    assert not pair.run().issues
    assert len(paths) == 2


def test_owned_interrupted_stage_recovered_unregistered_lookalike_preserved(pair):
    pair.enable()
    stage = pair.dst / ".obmanage-interrupted.tmp"
    stage.write_bytes(b"incomplete")
    with closing(RealtimeStore(pair.state)) as store:
        LiveCopyRecords(store, pair.token, str(pair.dst)).register_temp("", native(stage), identity(snapshot(stage)))
    assert not pair.run().issues
    assert not stage.exists()
    stage.write_bytes(b"not ours")
    assert stage.name in pair.run().issues and stage.read_bytes() == b"not ours"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes")
def test_source_still_open_for_writing_retries_while_other_notes_sync(pair):
    pair.enable()
    with (pair.src / "保留.md").open("wb") as output:
        output.write(b"working")
        output.flush()
        (pair.src / "new.md").write_bytes(b"saved")
        out = pair.run("保留.md", "new.md")
        assert "保留.md" in out.retry and (pair.dst / "保留.md").read_text() == "keep"
        assert (pair.dst / "new.md").read_bytes() == b"saved"
    assert not pair.run("保留.md").issues
    assert (pair.dst / "保留.md").read_bytes() == b"working"


def test_large_provable_directory_move_finishes_without_mass_delete_prompt(pair, monkeypatch):
    folder = pair.src / "draft"
    folder.mkdir()
    for i in range(15):
        (folder / str(i)).write_bytes(bytes([i]))
    pair.enable()
    folder.rename(pair.src / "finished")
    now = time.time()
    monkeypatch.setattr(live.time, "time", lambda: now)
    first = pair.run("draft")
    assert not first.issues and len(first.retry) == 16
    now += 10
    assert not pair.run(*first.retry).issues
    assert not (pair.dst / "draft").exists()
    assert all((pair.dst / "finished" / str(i)).read_bytes() == bytes([i]) for i in range(15))


def test_enable_missing_target_and_changed_plan_fails_before_overwrite(pair):
    pair.dst.rmdir()
    pair.enable()
    assert (pair.dst / "保留.md").read_text() == "keep"
    def change(plan):
        (pair.dst / "保留.md").write_text("foreign")
        return True
    with pytest.raises(SyncError):
        pair.enable(change)
    assert (pair.dst / "保留.md").read_text() == "foreign"


def test_volume_remount_uses_bound_directory_not_impostor_at_old_location(pair, monkeypatch):
    pair.enable()
    original = pair.dst
    other = original.with_name("remounted")
    original.rename(other)
    original.mkdir()
    monkeypatch.setattr(live, "portable_path", lambda path, locator: str(other))
    (pair.src / "new").write_bytes(b"note")
    assert not pair.run().issues
    assert not list(original.iterdir()) and (other / "new").read_bytes() == b"note"
    monkeypatch.setattr(live, "portable_path", lambda path, locator: str(original))
    with pytest.raises(SyncError):
        pair.run()
    assert not list(original.iterdir())


def test_protected_video_directory_survives_live_source_tree_removal(tmp_path, monkeypatch):
    pair = Pair(tmp_path, SYNC_MODE_NO_VIDEO)
    (pair.src / "media").mkdir()
    (pair.src / "media/note.md").write_bytes(b"doc")
    (pair.dst / "media").mkdir()
    (pair.dst / "media/movie.mp4").write_bytes(b"keep")
    pair.enable()
    (pair.src / "media/note.md").unlink()
    (pair.src / "media").rmdir()
    now = time.time()
    monkeypatch.setattr(live.time, "time", lambda: now)
    out = pair.run("media")
    now += 7
    assert not pair.run(*out.retry).issues
    assert list((pair.dst / "media").iterdir()) == [pair.dst / "media/movie.mp4"]


def test_empty_source_hold_survives_restart_and_a_new_unrelated_file(pair, monkeypatch):
    pair.enable()
    name = "保留.md"
    (pair.src / name).unlink()
    assert "有效来源为空" in pair.run(name).issues[""]
    monkeypatch.setattr(live, "DELETE_DELAY", 0)
    (pair.src / "new").write_bytes(b"new")
    assert name in pair.run().issues
    assert name in pair.run().issues and (pair.dst / name).read_text() == "keep"
    assert (pair.dst / "new").read_bytes() == b"new"
    (pair.dst / name).unlink()  # Explicitly resolved by a manual sync.
    assert not pair.run().issues


def test_event_alias_recovers_by_enumerating_real_names(pair, monkeypatch):
    pair.enable()
    (pair.src / "long-name.md").write_bytes(b"added")
    original = live._inventory
    def abbreviated(root, scopes, *args):
        if scopes:
            raise live._NeedInventory("short alias")
        return original(root, scopes, *args)
    monkeypatch.setattr(live, "_inventory", abbreviated)
    assert not pair.run("LONG-N~1.MD").issues
    assert (pair.dst / "long-name.md").read_bytes() == b"added"


@pytest.mark.skipif(os.name != "nt", reason="Windows short path spelling")
def test_configured_short_root_alias_keeps_its_binding(pair):
    def short(path):
        buf = ctypes.create_unicode_buffer(32768)
        assert ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
        return buf.value
    source, target = short(pair.src), short(pair.dst)
    token = live.enable_realtime(pair.state, source, target, pair.mode, pair.cancel, None, lambda _: True)
    (pair.src / "after.md").write_text("saved")
    out = live.reconcile(pair.state, source, target, pair.mode, token, ("after.md",), pair.cancel)
    assert not out.issues and (pair.dst / "after.md").read_text() == "saved"


def test_partial_result_reports_actual_copy_before_late_root_failure(pair, monkeypatch):
    pair.enable()
    dest = pair.dst / "new"
    (pair.src / dest.name).write_bytes(b"copied")
    original = live._check_binding
    def late(roots):
        if dest.exists():
            raise SyncError("根身份改变")
        original(roots)
    monkeypatch.setattr(live, "_check_binding", late)
    out = pair.run("new")
    assert out.copies.copied_files == 1 and "部分完成" in out.issues[""] and "" in out.retry


@pytest.mark.skipif(os.name != "nt", reason="Windows recursive notification")
def test_directory_mtime_hint_does_not_expand_an_ordinary_file_save(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    file = folder / "note.md"
    file.write_bytes(b"old")
    with closing(DirectoryWatch(tmp_path)) as watch:
        file.write_bytes(b"new")
        observed = set()
        until = time.monotonic() + 3
        while time.monotonic() < until and "notes/note.md" not in observed:
            observed.update(watch.poll())
            time.sleep(.01)
        assert observed == {"notes/note.md"}

def test_auto_overlap_allows_shared_source_only(tmp_path):
    a, b, c = (str(tmp_path / n) for n in "abc")
    assert not live.automatic_overlap(a, b, a, c)
    assert live.automatic_overlap(a, b, c, os.path.join(b, "nested"))
    assert live.automatic_overlap(a, b, b, c)


def test_overlapping_competing_job_stops_engine_before_enable_and_before_copy(pair):
    competing = ((str(pair.dst), str(pair.dst.with_name("other")), None),)
    with pytest.raises(SyncError, match="重叠"):
        live.enable_realtime(pair.state, str(pair.src), str(pair.dst), pair.mode, pair.cancel,
                             None, lambda _: pytest.fail("overlap reached confirmation"), automatic=competing)
    assert not list(pair.dst.iterdir())
    pair.enable()
    (pair.src / "new").write_text("new")
    with pytest.raises(SyncError, match="重叠"):
        live.reconcile(pair.state, str(pair.src), str(pair.dst), pair.mode, pair.token, ("new",),
                       pair.cancel, automatic=competing)
    assert not (pair.dst / "new").exists()


def test_incomplete_notification_requires_full_check():
    assert changed_names(b"") == {""}
    assert changed_names(struct.pack("<III", 0, 3, 8) + b"x") == {""}
    name = "../out".encode("utf-16-le")
    assert changed_names(struct.pack("<III", 0, 3, len(name)) + name) == {""}


@pytest.mark.skipif(os.name != "nt", reason="Windows recursive notification")
def test_windows_watcher_recurses_renames_and_releases_handles(tmp_path):
    folder = tmp_path / "目录"
    folder.mkdir()
    with closing(DirectoryWatch(tmp_path)) as watch:
        assert not watch.poll()
        note = folder / "旧.md"
        note.write_text("one")
        note.rename(folder / "新.md")
        observed = set()
        until = time.monotonic() + 3
        while time.monotonic() < until:
            observed.update(watch.poll())
            if {"目录/旧.md", "目录/新.md"} <= observed:
                break
            time.sleep(.01)
        assert {"目录/旧.md", "目录/新.md"} <= observed
    folder.rename(tmp_path / "after")
