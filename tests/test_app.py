from __future__ import annotations

import time

from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

from obmanage.app import (
    acquire_instance_lock,
    create_instance_focus_server,
    instance_focus_server_name,
    main,
    notify_existing_instance,
)


def test_second_instance_notifies_running_window_to_focus(tmp_path):
    app = QApplication.instance() or QApplication([])
    state_dir = tmp_path / "state"
    calls: list[str] = []
    server = create_instance_focus_server(state_dir, lambda: calls.append("focus"))
    assert server is not None
    try:
        assert notify_existing_instance(state_dir)
        deadline = time.monotonic() + 1.0
        while not calls and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert calls == ["focus"]
    finally:
        server.close()
        QLocalServer.removeServer(instance_focus_server_name(state_dir))


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


def test_startup_checks_incremental_endpoints_before_creating_logs(tmp_path, monkeypatch):
    from obmanage import app as app_module
    from obmanage.settings import SettingsDocument, SettingsStore

    collection = tmp_path / "collection"
    state = collection / "state"
    document = SettingsDocument(features={"incremental": {
        "local_path": str(collection), "portable_path": str(tmp_path / "portable"),
    }})
    SettingsStore(state).save_document(document)
    before = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
    messages = []
    monkeypatch.setattr(app_module, "_report_startup_rejection", lambda message, **kwargs: messages.append(message))
    assert main(["--state-dir", str(state)]) == 2
    assert "程序数据目录" in messages[0]
    assert {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()} == before
