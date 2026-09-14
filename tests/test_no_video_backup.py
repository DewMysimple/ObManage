from __future__ import annotations

import os
from pathlib import Path

import pytest

import obmanage.engine as engine_module
from obmanage.engine import SYNC_MODE_NO_VIDEO, SyncEngine


def put(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def backup_roots(tmp_path):
    portable = tmp_path / "移动硬盘完整仓库"
    laptop = tmp_path / "笔记本轻量仓库"
    portable.mkdir()
    laptop.mkdir()
    return SyncEngine(tmp_path / "state"), portable, laptop


def analyze(engine: SyncEngine, source: Path, target: Path, **kwargs):
    return engine.analyze(
        str(source), str(target), mode=SYNC_MODE_NO_VIDEO, **kwargs
    )


def test_full_vault_to_laptop_copies_non_video_and_preserves_all_videos(backup_roots):
    engine, portable, laptop = backup_roots
    put(portable, ".obsidian/app.json", b'{"theme":"dark"}')
    put(portable, "笔记/今日.md", "正文".encode())
    put(portable, "附件/课程.MP4", b"portable source video")
    put(laptop, "旧笔记.md", b"remove this non-video file")
    kept_video = put(laptop, "仅笔记本视频/本地.webm", b"keep target-only video")

    plan = analyze(engine, portable, laptop)

    assert plan.mode == SYNC_MODE_NO_VIDEO
    assert plan.can_execute
    assert plan.counts["exclude"] == 2
    assert plan.excluded_source_files == 1
    assert plan.excluded_target_files == 1
    assert plan.counts["add"] == 2
    assert plan.counts["delete"] == 1
    assert not any(
        item.action == "rmdir" and item.relative_path == "仅笔记本视频"
        for item in plan.items
    )

    result = engine.execute(plan)

    assert result.status == "success", result.errors
    assert (laptop / ".obsidian/app.json").read_bytes() == b'{"theme":"dark"}'
    assert (laptop / "笔记/今日.md").read_text(encoding="utf-8") == "正文"
    assert not (laptop / "旧笔记.md").exists()
    assert not (laptop / "附件/课程.MP4").exists()
    assert kept_video.read_bytes() == b"keep target-only video"


def test_laptop_to_full_vault_applies_non_video_edits_without_touching_video(backup_roots):
    engine, portable, laptop = backup_roots
    video = put(portable, "媒体/演示.mkv", b"full vault video")
    put(portable, "笔记.md", b"old note")
    put(portable, "删除我.pdf", b"obsolete")
    put(laptop, "笔记.md", b"edited on laptop")
    put(laptop, "新增.canvas", b"{}")
    video_before = (video.stat().st_ino, video.stat().st_mtime_ns, video.read_bytes())

    plan = analyze(engine, laptop, portable)
    result = engine.execute(plan)

    assert result.status == "success", result.errors
    assert (portable / "笔记.md").read_bytes() == b"edited on laptop"
    assert (portable / "新增.canvas").read_bytes() == b"{}"
    assert not (portable / "删除我.pdf").exists()
    assert (video.stat().st_ino, video.stat().st_mtime_ns, video.read_bytes()) == video_before
    assert (portable / "媒体").is_dir()


def test_excluded_video_is_never_hashed_and_typescript_is_not_excluded(
    backup_roots, monkeypatch
):
    engine, portable, laptop = backup_roots
    put(portable, "same.mp4", b"source version")
    put(laptop, "same.mp4", b"target differs")
    put(portable, "plugin.ts", b"export const value = 1")

    real_hash_pair = engine_module._hash_pair

    def checked_hash_pair(source_path, *args, **kwargs):
        assert not str(source_path).casefold().endswith(".mp4")
        return real_hash_pair(source_path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_hash_pair", checked_hash_pair)
    plan = analyze(engine, portable, laptop, deep=True)

    assert plan.counts["exclude"] == 1
    assert plan.counts["add"] == 1
    result = engine.execute(plan)
    assert result.status == "success", result.errors
    assert (laptop / "same.mp4").read_bytes() == b"target differs"
    assert (laptop / "plugin.ts").read_bytes() == b"export const value = 1"


def test_filtered_baselines_are_isolated_from_complete_mirror(backup_roots):
    engine, portable, laptop = backup_roots
    put(portable, "note.md", b"same")
    put(laptop, "note.md", b"same")

    complete = engine.analyze(str(portable), str(laptop))
    filtered = analyze(engine, portable, laptop)

    assert complete.pair_id != filtered.pair_id
    assert complete.context["base_pair_id"] == filtered.context["base_pair_id"]
    assert complete.mode == "mirror"
    assert filtered.mode == SYNC_MODE_NO_VIDEO


def test_video_only_source_requires_empty_confirmation_for_non_video_deletion(
    backup_roots
):
    engine, portable, laptop = backup_roots
    put(portable, "课程.mp4", b"video")
    put(laptop, "old.md", b"delete only after empty confirmation")
    kept = put(laptop, "keep.mov", b"always keep video")

    plan = analyze(engine, portable, laptop)

    assert plan.source_empty
    assert engine.execute(plan).status == "failed"
    assert (laptop / "old.md").exists()
    result = engine.execute(plan, allow_empty=True)
    assert result.status == "success", result.errors
    assert not (laptop / "old.md").exists()
    assert kept.read_bytes() == b"always keep video"


def test_plan_mode_tampering_is_rejected(backup_roots):
    engine, portable, laptop = backup_roots
    put(portable, "note.md", b"content")
    plan = analyze(engine, portable, laptop)
    plan.mode = "mirror"

    result = engine.execute(plan)

    assert result.status == "failed"
    assert not (laptop / "note.md").exists()


def test_complete_mirror_still_copies_video(backup_roots):
    engine, portable, laptop = backup_roots
    put(portable, "course.mp4", b"video bytes")

    plan = engine.analyze(str(portable), str(laptop))
    result = engine.execute(plan)

    assert result.status == "success", result.errors
    assert (laptop / "course.mp4").read_bytes() == b"video bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison is case-insensitive")
def test_case_only_parent_name_is_not_changed_when_it_would_move_a_video(backup_roots):
    engine, portable, laptop = backup_roots
    put(portable, "Media/note.md", b"note")
    put(portable, "Media/course.mp4", b"source video")
    video = put(laptop, "media/course.mp4", b"target video")
    before = (video.stat().st_ino, video.stat().st_mtime_ns, video.read_bytes())

    plan = analyze(engine, portable, laptop)

    assert not any(
        item.action == "rename" and item.relative_path == "Media"
        for item in plan.items
    )
    result = engine.execute(plan)
    assert result.status == "success", result.errors
    assert (laptop / "media/note.md").read_bytes() == b"note"
    assert [path.name for path in laptop.iterdir()] == ["media"]
    assert (video.stat().st_ino, video.stat().st_mtime_ns, video.read_bytes()) == before
