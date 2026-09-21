from __future__ import annotations

import pytest

import obmanage.engine as module
from obmanage.engine import SyncEngine, _require_manifest
from obmanage.models import SyncError
from test_engine import put
from test_roundtrip import forbid_mutations, immutable_snapshot


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("side", ["source", "target"])
def test_preflight_reports_actual_side_path_and_metadata_without_writes(tmp_path, monkeypatch, reverse, side):
    left, right = tmp_path / "left", tmp_path / "right"
    put(left, ".obsidian/workspace.json", b"left")
    put(right, ".obsidian/workspace.json", b"right")
    engine = SyncEngine(tmp_path / "state")
    plan = engine.analyze_pair(str(left), str(right))[int(reverse)]
    origin, destination = (right, left) if reverse else (left, right)
    changed = origin if side == "source" else destination
    put(changed, ".obsidian/workspace.json", b"external update")
    before = immutable_snapshot(left), immutable_snapshot(right)
    forbid_mutations(monkeypatch, left)
    forbid_mutations(monkeypatch, right)
    result = engine.execute(plan)
    assert result.status == "failed"
    error = result.errors[0]
    assert ("源端" if side == "source" else "目标端") in error
    assert ".obsidian/workspace.json" in error and "大小（字节）" in error
    assert "不代表已确认正文变化" in error
    assert (immutable_snapshot(left), immutable_snapshot(right)) == before


def test_manifest_diagnostics_classify_changes_bound_output_and_escape_names():
    state = dict(kind="file", size=3, mtime_ns=1, ctime_ns=2, inode=3, device=4)
    old = {"gone.md": state, "changed.md": state}
    new = {"added\n[forged].md": state,
           "changed.md": dict(state, kind="dir", size=0, inode=5, mtime_ns=9)}
    with pytest.raises(SyncError) as caught:
        _require_manifest(old, new, root="root", side="目标端", message="预览过期")
    error = str(caught.value)
    assert "新增" in error and "消失或改名" in error
    assert "类型 file → dir" in error and "文件标识 3 → 5" in error
    assert "修改时间（纳秒） 1 → 9" in error
    assert "\n" not in error and r"\n[forged]" in error
    with pytest.raises(SyncError) as caught:
        _require_manifest({}, {f"{i:02}.md": state for i in range(12)},
                          root="root", side="源端", message="预览过期")
    assert "12 项" in str(caught.value) and "另有 4 项未列出" in str(caught.value)
    assert '"07.md"' in str(caught.value) and '"08.md"' not in str(caught.value)


@pytest.mark.parametrize("side", ["source", "target"])
def test_analysis_final_barrier_reports_change_without_an_extra_scan(tmp_path, monkeypatch, side):
    source, target = tmp_path / "source", tmp_path / "target"
    put(source, "note.md", b"same")
    put(target, "note.md", b"same")
    original = module._scan
    calls = []
    def changed_scan(root, *args, **kwargs):
        calls.append(root)
        if len(calls) == (3 if side == "source" else 4):
            put(source if side == "source" else target, "new.md", b"new")
        return original(root, *args, **kwargs)
    monkeypatch.setattr(module, "_scan", changed_scan)
    plan = SyncEngine(tmp_path / "state").analyze(str(source), str(target))
    assert not plan.can_execute
    assert "new.md" in plan.errors[0] and "新增" in plan.errors[0]
    assert ("源端" if side == "source" else "目标端") in plan.errors[0]
    assert len(calls) == (3 if side == "source" else 4)
