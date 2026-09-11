from __future__ import annotations

from obmanage.app import acquire_instance_lock, main


def test_preview_uses_same_instance_lock_as_desktop_app(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    existing = acquire_instance_lock(state_dir)
    assert existing is not None
    try:
        exit_code = main([
            "--state-dir", str(state_dir),
            "--preview",
            "--source", str(tmp_path / "source"),
            "--target", str(tmp_path / "target"),
        ])
    finally:
        existing.unlock()

    assert exit_code == 2
    assert "已有任务运行中" in capsys.readouterr().err
    available_again = acquire_instance_lock(state_dir)
    assert available_again is not None
    available_again.unlock()


def test_preview_rejects_state_inside_source_before_any_write(tmp_path, capsys):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "note.md").write_text("source", encoding="utf-8")
    state_dir = source / "app-state"
    before = tuple(sorted(path.relative_to(source) for path in source.rglob("*")))

    exit_code = main([
        "--state-dir", str(state_dir),
        "--preview",
        "--source", str(source),
        "--target", str(target),
    ])

    assert exit_code == 2
    assert "程序数据目录" in capsys.readouterr().err
    assert not state_dir.exists()
    assert tuple(sorted(path.relative_to(source) for path in source.rglob("*"))) == before


def test_preview_closes_logging_handle_before_return(tmp_path, capsys):
    source = tmp_path / "source"
    target = tmp_path / "target"
    state_dir = tmp_path / "state"
    source.mkdir()
    target.mkdir()
    (source / "note.md").write_text("source", encoding="utf-8")

    assert main([
        "--state-dir", str(state_dir),
        "--preview",
        "--source", str(source),
        "--target", str(target),
    ]) == 0
    capsys.readouterr()

    log_path = state_dir / "obmanage.log"
    moved = state_dir / "closed.log"
    log_path.replace(moved)
    assert moved.is_file()
