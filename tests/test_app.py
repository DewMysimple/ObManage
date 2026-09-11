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
