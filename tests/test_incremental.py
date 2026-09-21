from __future__ import annotations

import os
from pathlib import Path
from threading import Event

import pytest

import obmanage.management.incremental as module
from obmanage.management.incremental import IncrementalEngine, selected_plans
from obmanage.models import SyncCancelled, SyncError, SyncResult


def vault(root: Path, name: str, files: dict[str, bytes]) -> Path:
    target = root / name
    (target / ".obsidian").mkdir(parents=True, exist_ok=True)
    for relative, data in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return target


def tree(root: Path):
    return {str(path.relative_to(root)): ("dir" if path.is_dir() else path.read_bytes())
            for path in root.rglob("*")}


@pytest.fixture
def collections(tmp_path):
    local, portable = tmp_path / "电脑", tmp_path / "移动硬盘"
    local.mkdir()
    portable.mkdir()
    return local, portable, IncrementalEngine(tmp_path / "state")


def test_mixed_directions_and_deletions_are_user_choices_and_sources_remain_untouched(collections, monkeypatch):
    local, portable, engine = collections
    vault(local, "A", {"note.md": b"old-A", "obsolete.md": b"delete me"})
    vault(portable, "A", {"note.md": b"new-A", "video.mp4": b"full video"})
    vault(local, "B", {"note.md": b"new-B"})
    vault(portable, "B", {"note.md": b"old-B", "old/old.md": b"delete"})
    for root in (local, portable):
        vault(root, "same", {"note.md": b"equal"})
    vault(local, "unselected", {"note.md": b"keep local"})
    vault(portable, "unselected", {"note.md": b"keep portable"})
    before = tree(local), tree(portable)
    analysis = engine.analyze(str(local), str(portable))
    assert (tree(local), tree(portable)) == before
    assert analysis.identical_count == 1
    assert {pair.key for pair in analysis.pairs} == {"a", "b", "unselected"}
    # Trap even transient writes/deletes/renames to either chosen source.
    from test_roundtrip import forbid_mutations, immutable_snapshot
    source_before = immutable_snapshot(portable / "A"), immutable_snapshot(local / "B")
    forbid_mutations(monkeypatch, portable / "A")
    forbid_mutations(monkeypatch, local / "B")
    progress = []
    result = engine.execute(analysis, {"a": "portable", "b": "local"}, progress=progress.append)
    assert result.status == "success", result.errors
    assert len(result.outcomes) == 2
    byte_progress = [event.completed_bytes for event in progress if event.total_bytes]
    assert byte_progress == sorted(byte_progress)
    assert (immutable_snapshot(portable / "A"), immutable_snapshot(local / "B")) == source_before
    assert tree(local / "A") == tree(portable / "A")
    assert tree(local / "B") == tree(portable / "B")
    assert (local / "unselected/note.md").read_bytes() == b"keep local"
    assert (portable / "unselected/note.md").read_bytes() == b"keep portable"
    assert {pair.key for pair in engine.analyze(str(local), str(portable)).pairs} == {"unselected"}


def test_fewer_files_can_be_selected_as_source(collections):
    local, portable, engine = collections
    vault(local, "A", {"keep.md": b"keep"})
    vault(portable, "A", {"keep.md": b"keep", "extra.md": b"remove"})
    analysis = engine.analyze(str(local), str(portable))
    with pytest.raises(SyncError, match="选择"):
        selected_plans(analysis, {})
    assert engine.execute(analysis, {"a": "local"}).status == "success"
    assert not (portable / "A/extra.md").exists()


def test_content_comparison_ignores_timestamps_and_old_baselines(collections):
    local, portable, engine = collections
    for root in (local, portable):
        vault(root, "A", {"note.md": b"same"})
    os.utime(portable / "A/note.md", ns=(1_000_000_000, 1_000_000_000))
    assert engine.analyze(str(local), str(portable)).identical_count == 1
    path = local / "A/note.md"
    stamp = path.stat()
    path.write_bytes(b"diff")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    analysis = engine.analyze(str(local), str(portable), deep=True)
    assert len(analysis.pairs) == 1
    assert analysis.pairs[0].local_plan.counts["update"] == 1
    assert analysis.pairs[0].portable_plan.counts["update"] == 1


def test_warm_scan_reuses_equality_and_compares_changed_files(collections, monkeypatch):
    import obmanage.engine as implementation
    local, portable, engine = collections
    for root in (local, portable):
        vault(root, "A", {"same.mp4": b"video", "change.md": b"old"})
    assert engine.analyze(str(local), str(portable)).identical_count == 1
    original = implementation._hash_pair
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[6])
        return original(*args, **kwargs)
    monkeypatch.setattr(implementation, "_hash_pair", counted)
    assert engine.analyze(str(local), str(portable)).identical_count == 1
    assert calls == []
    path = local / "A/change.md"
    path.write_bytes(b"new")
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
    analysis = engine.analyze(str(local), str(portable))
    assert calls == ["change.md"]  # mismatch is not hashed again for reverse
    assert analysis.pairs[0].local_plan.counts["update"] == 1
    assert analysis.pairs[0].portable_plan.counts["update"] == 1
    calls.clear()
    engine.analyze(str(local), str(portable), deep=True)
    assert sorted(calls) == ["change.md", "same.mp4"]


def test_relative_paths_pair_duplicate_names_and_missing_vaults(collections):
    local, portable, engine = collections
    for group in ("one", "two"):
        vault(local, f"{group}/A", {"x.md": group.encode()})
        vault(portable, f"{group}/A", {"x.md": b"old"})
    vault(local, "new", {"x.md": b"new"})
    analysis = engine.analyze(str(local), str(portable))
    assert {pair.key for pair in analysis.pairs} == {"one/a", "two/a", "new"}
    pair = next(pair for pair in analysis.pairs if pair.key == "new")
    assert pair.portable_plan is None
    assert engine.execute(analysis, {"new": "portable"}).status == "failed"
    assert not (portable / "new").exists()
    assert engine.execute(analysis, {"new": "local"}).status == "success"
    assert tree(local / "new") == tree(portable / "new")


def test_existing_non_vault_target_is_not_authorized(collections):
    local, portable, engine = collections
    vault(local, "A", {"x.md": b"new"})
    (portable / "A").mkdir()
    (portable / "A/private.txt").write_bytes(b"keep")
    analysis = engine.analyze(str(local), str(portable))
    assert analysis.pairs[0].errors
    assert engine.execute(analysis, {"a": "local"}).status == "failed"
    assert (portable / "A/private.txt").read_bytes() == b"keep"


@pytest.mark.parametrize("side", ["local", "portable"])
def test_stale_later_preview_blocks_all_writes(collections, side):
    local, portable, engine = collections
    for name in ("A", "B"):
        vault(local, name, {"x.md": b"new"})
        vault(portable, name, {"x.md": b"old"})
    analysis = engine.analyze(str(local), str(portable))
    changed = local if side == "local" else portable
    (changed / "B/x.md").write_bytes(b"changed after analysis")
    before = tree(local), tree(portable)
    result = engine.execute(analysis, {"a": "local", "b": "local"})
    assert result.status == "failed"
    assert result.outcomes == []
    assert (tree(local), tree(portable)) == before
    assert "执行前复核失败，仓库 B" in result.errors[0]
    assert "x.md" in result.errors[0]
    assert ("源端" if side == "local" else "目标端") in result.errors[0]


def test_nested_selection_cannot_overwrite_another_selected_source(collections):
    local, portable, engine = collections
    for root, value in ((local, b"local"), (portable, b"portable")):
        vault(root, "parent", {"x.md": value})
        vault(root, "parent/child", {"x.md": value})
    analysis = engine.analyze(str(local), str(portable))
    before = tree(local), tree(portable)
    result = engine.execute(analysis, {"parent": "local", "parent/child": "portable"})
    assert result.status == "failed"
    assert "重叠" in result.errors[0]
    assert (tree(local), tree(portable)) == before
    assert engine.execute(analysis, {"parent/child": "portable"}).status == "success"
    assert (local / "parent/x.md").read_bytes() == b"local"


@pytest.mark.parametrize("failure", ["failed", "cancelled"])
def test_stops_after_first_incomplete_vault(collections, monkeypatch, failure):
    local, portable, engine = collections
    for name in ("A", "B"):
        vault(local, name, {"x.md": b"new"})
        vault(portable, name, {"x.md": b"old"})
    analysis = engine.analyze(str(local), str(portable))
    calls = []

    def incomplete(self, plan, **kwargs):
        calls.append(plan.source)
        return SyncResult(failure, copied_files=1, errors=["injected"])

    monkeypatch.setattr(module.SyncEngine, "execute", incomplete)
    result = engine.execute(analysis, {"a": "local", "b": "local"})
    assert result.status == failure
    assert len(calls) == 1
    assert result.outcomes[0].result.copied_files == 1
    assert (portable / "B/x.md").read_bytes() == b"old"


def test_cancel_and_recovery_gate_are_read_only(collections, monkeypatch):
    local, portable, engine = collections
    vault(local, "A", {"x.md": b"new"})
    vault(portable, "A", {"x.md": b"old"})
    cancelled = Event()
    cancelled.set()
    with pytest.raises(SyncCancelled):
        engine.analyze(str(local), str(portable), cancel=cancelled)
    analysis = engine.analyze(str(local), str(portable))
    assert engine.execute(analysis, {"a": "local"}, cancel=cancelled).status == "cancelled"

    def unreadable(self):
        raise SyncError("恢复记录无法读取")

    monkeypatch.setattr(module.DeploymentEngine, "list_batches", unreadable)
    result = engine.execute(analysis, {"a": "local"})
    assert result.status == "failed"
    assert result.outcomes == []
    assert (portable / "A/x.md").read_bytes() == b"old"


def test_state_and_collection_overlap_rejected_without_writes(collections):
    local, portable, engine = collections
    with pytest.raises(SyncError):
        engine.analyze(str(local), str(local / "child"))
    with pytest.raises(SyncError):
        IncrementalEngine(local / "state").analyze(str(local), str(portable))
    assert not (local / "state").exists()


def test_real_copy_failure_preserves_prior_results_stops_deletions_and_later_vaults(collections, monkeypatch):
    local, portable, engine = collections
    for name in ("A", "B", "C"):
        vault(local, name, {"x.md": b"new"})
        vault(portable, name, {"x.md": b"old", "delete.md": b"extra"})
    analysis = engine.analyze(str(local), str(portable))
    original = module.SyncEngine._verify_commit_copy

    def fail_second(self, plan, *args, **kwargs):
        if Path(plan.source).name == "B":
            raise SyncError("injected target verification failure")
        return original(self, plan, *args, **kwargs)

    monkeypatch.setattr(module.SyncEngine, "_verify_commit_copy", fail_second)
    result = engine.execute(analysis, {key: "local" for key in ("a", "b", "c")})
    assert result.status == "failed"
    assert [item.result.status for item in result.outcomes] == ["success", "failed"]
    assert tree(local / "A") == tree(portable / "A")
    for name in ("B", "C"):
        assert (portable / name / "x.md").read_bytes() == b"old"
        assert (portable / name / "delete.md").read_bytes() == b"extra"
        assert set(tree(portable / name)) == {".obsidian", "x.md", "delete.md"}


def test_link_in_collection_is_visible_and_blocks_incomplete_batch(collections):
    local, portable, engine = collections
    vault(local, "A", {"x.md": b"new"})
    vault(portable, "A", {"x.md": b"old"})
    link = local / "alias"
    try:
        link.symlink_to(local / "A", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink unavailable: {exc}")
    analysis = engine.analyze(str(local), str(portable))
    assert analysis.issues
    assert engine.execute(analysis, {"a": "local"}).status == "failed"
    assert (portable / "A/x.md").read_bytes() == b"old"


def test_legacy_recovery_blocks_new_batch(collections, tmp_path, seed_legacy_quarantine):
    local, portable, engine = collections
    vault(local, "A", {"x.md": b"new"})
    vault(portable, "A", {"x.md": b"old"})
    legacy = vault(tmp_path / "legacy", "vault", {".trash/old.md": b"trash"})
    seed_legacy_quarantine(engine.state_dir, legacy)
    analysis = engine.analyze(str(local), str(portable))
    result = engine.execute(analysis, {"a": "local"})
    assert result.status == "failed"
    assert "回收站" in result.errors[0]
    assert (portable / "A/x.md").read_bytes() == b"old"
