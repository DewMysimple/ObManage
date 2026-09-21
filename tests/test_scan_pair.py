from __future__ import annotations

import os
from pathlib import Path

import pytest

import obmanage.engine as module
from obmanage.engine import SyncEngine
from test_engine import put
from test_roundtrip import forbid_mutations, immutable_snapshot


@pytest.mark.parametrize("reverse", [False, True])
def test_pair_one_epoch_matches_independent_plans_and_executes(tmp_path, monkeypatch, reverse):
    source, target = tmp_path / "source", tmp_path / "target"
    put(source, "Notes/Same.md", b"same")
    put(target, "notes/same.md", b"same")
    put(source, "changed.md", b"new!")
    put(target, "changed.md", b"old!")
    put(source, "only-source.md", b"source")
    put(target, "only-target.md", b"target")
    (source / "empty-source").mkdir()
    (target / "empty-target").mkdir()
    engine = SyncEngine(tmp_path / "state")
    original_scan, original_hash = module._scan, module._hash_pair
    scans, hashes = [], []
    def scan(*args, **kwargs):
        scans.append(args[0])
        return original_scan(*args, **kwargs)
    def hash_pair(*args, **kwargs):
        hashes.append(args[6])
        return original_hash(*args, **kwargs)
    monkeypatch.setattr(module, "_scan", scan)
    monkeypatch.setattr(module, "_hash_pair", hash_pair)
    forward, backward = engine.analyze_pair(str(source), str(target), deep=True)
    assert len(scans) == 4  # initial + final manifest barrier, both sides
    assert len(hashes) == (2 if os.name == "nt" else 1)
    def actions(plan):
        return sorted((item.action, item.relative_path, item.size) for item in plan.items)
    assert actions(backward) == actions(engine.analyze(str(target), str(source), deep=True))
    plan = backward if reverse else forward
    origin, destination = (target, source) if reverse else (source, target)
    before = immutable_snapshot(origin)
    forbid_mutations(monkeypatch, origin)
    result = engine.execute(plan)
    assert result.status == "success", result.errors
    assert immutable_snapshot(origin) == before
    assert not engine.analyze(str(origin), str(destination)).has_changes


@pytest.mark.skipif(os.name != "nt", reason="Windows case-only rename")
@pytest.mark.parametrize("fail_second", [False, True])
def test_exfat_successful_noop_rename_uses_safe_hop(tmp_path, monkeypatch, fail_second):
    source, target = tmp_path / "source", tmp_path / "target"
    put(source, "Chapter2/Attachments/note.md", b"same")
    put(target, "Chapter2/attachments/note.md", b"same")
    put(target, "extra.md", b"keep until all renames succeed")
    engine = SyncEngine(tmp_path / "state")
    plan = engine.analyze(str(source), str(target))
    before = immutable_snapshot(source)
    forbid_mutations(monkeypatch, source)
    original = os.rename
    def exfat_rename(old, new):
        if str(old).casefold() == str(new).casefold():
            return  # exFAT reports success but keeps old spelling
        if fail_second and Path(old).name != "attachments" and Path(new).name == "Attachments":
            raise PermissionError("injected second move failure")
        return original(old, new)
    monkeypatch.setattr(module.os, "rename", exfat_rename)
    result = engine.execute(plan)
    assert immutable_snapshot(source) == before
    assert not list(target.rglob(".obmanage-case-*"))
    if fail_second:
        assert result.status == "failed"
        assert (target / "extra.md").exists()
        assert [p.name for p in (target / "Chapter2").iterdir()] == ["attachments"]
    else:
        assert result.status == "success", result.errors
        assert not (target / "extra.md").exists()
        assert [p.name for p in (target / "Chapter2").iterdir()] == ["Attachments"]
        assert not engine.analyze(str(source), str(target)).has_changes


def test_pair_conflicts_remain_non_executable(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    put(source, "conflict", b"file")
    (target / "conflict").mkdir(parents=True)
    plans = SyncEngine(tmp_path / "state").analyze_pair(str(source), str(target))
    assert all(not plan.can_execute for plan in plans)


@pytest.mark.skipif(os.name != "nt", reason="Windows case-only rename")
@pytest.mark.parametrize("collision", [False, True])
def test_failed_case_hop_preserves_retained_data_and_foreign_target(tmp_path, monkeypatch, collision):
    source, target = tmp_path / "source", tmp_path / "target"
    put(source, "Attachments/note.md", b"same")
    put(target, "attachments/note.md", b"same")
    put(target, "extra.md", b"must not delete")
    engine = SyncEngine(tmp_path / "state")
    plan = engine.analyze(str(source), str(target))
    original = os.rename
    retained = []
    def interrupted(old, new):
        if str(old).casefold() == str(new).casefold():
            return
        if Path(old).name == "attachments":
            original(old, new)
            retained.append(Path(new))
            if collision:
                put(target, "Attachments/foreign.md", b"foreign content")
            return
        raise PermissionError("second hop and restoration unavailable")
    monkeypatch.setattr(module.os, "rename", interrupted)
    result = engine.execute(plan)
    assert result.status == "failed"
    assert (retained[0] / "note.md").read_bytes() == b"same"
    assert module.canonical(retained[0]) in result.errors[0]
    assert (target / "extra.md").read_bytes() == b"must not delete"
    if collision:
        assert (target / "Attachments/foreign.md").read_bytes() == b"foreign content"
