import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from obmanage.pages import realtime as ui_live
from obmanage.ui import MainWindow
from test_ui import pump_until, wait_for_confirmation


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def dispose(app, window):
    window._timer.stop()
    for panel in window.live_panels.values():
        panel.close_watch()
    if window.busy:
        window.cancel_operation()
        pump_until(app, lambda: not window.busy)
    window._save_timer.stop()
    window.hide()
    window.tray.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def shell(app, tmp_path):
    window = MainWindow(tmp_path / "state")
    window._timer.stop()
    local = tmp_path / "local"
    local.mkdir()
    (local / "note.md").write_text("initial")
    portable = tmp_path / "portable"
    backup = tmp_path / "backup"
    portable.mkdir()
    backup.mkdir()
    window.local_edit.setText(str(local))
    window.portable_combo.setEditText(str(portable))
    page = window.pages["vault_backup"]
    page.local_picker.set_value(str(local))
    page.portable_picker.set_value(str(backup))
    window.show()
    app.processEvents()
    yield window, local, portable, backup
    dispose(app, window)


def enable(app, window, key):
    window._show_page(key)
    panel = window.live_panels[key]
    panel.toggle.click()
    confirmation = wait_for_confirmation(app, window)
    assert confirmation.windowTitle() == "开启实时同步"
    confirmation.accept()
    pump_until(app, lambda: not window.busy)
    assert panel.enabled, panel.label.text()
    return panel


def tick(app, window):
    window._timer_tick()
    pump_until(app, lambda: not window.busy)


@pytest.mark.parametrize("key", ["mirror", "vault_backup"])
def test_native_saved_event_to_target_and_tray_without_extra_confirmation(app, shell, key, monkeypatch):
    window, local, mirror, backup = shell
    dest = mirror if key == "mirror" else backup
    panel = enable(app, window, key)
    tick(app, window)
    assert not panel.pending and not panel.issues
    assert panel.last_success
    monkeypatch.setattr(window.tray, "isVisible", lambda: True)
    monkeypatch.setattr(window, "_notify", lambda *args: None)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted() and not window.isVisible() and not window._exit_requested
    (local / "note.md").write_text("saved once")
    (local / "note.md").write_text("final saved version")
    pump_until(app, lambda: (panel.observe(time.monotonic()), "note.md" in panel.pending)[1])
    tick(app, window)
    assert (dest / "note.md").read_text() == "initial"
    panel.pending["note.md"] = 0
    tick(app, window)
    assert (dest / "note.md").read_text() == "final saved version"
    assert "已同步" in panel.summary
    panel.toggle.click()
    assert not panel.enabled and panel.watch is None


def test_both_profiles_fair_queue_video_isolation_overlap_and_timer(app, shell):
    window, local, mirror, backup = shell
    (local / "clip.MP4").write_bytes(b"video")
    first = enable(app, window, "mirror")
    second = enable(app, window, "vault_backup")
    assert first.config["token"] != second.config["token"]
    tick(app, window)
    tick(app, window)
    assert not first.pending and not second.pending
    assert (mirror / "clip.MP4").exists() and not (backup / "clip.MP4").exists()
    window.schedule_toggle.setChecked(True)
    assert not window.schedule_toggle.isChecked()
    window.pages["vault_backup"].portable_picker.set_value(str(mirror / "nested"))
    assert not second.enabled and first.enabled
    second.toggle.click()
    assert "重叠" in second.label.text() and not window.busy


def test_inflight_save_survives_and_recovery_retains_pending(app, shell, monkeypatch):
    window, local, target, _ = shell
    panel = enable(app, window, "mirror")
    tick(app, window)
    release = threading.Event()
    started = threading.Event()
    original = ui_live.reconcile
    def blocking(*args, **kwargs):
        value = original(*args, **kwargs)
        started.set()
        assert release.wait(4)
        return value
    monkeypatch.setattr(ui_live, "reconcile", blocking)
    try:
        (local / "note.md").write_text("one")
        panel.check_now()
        window._timer_tick()
        pump_until(app, started.is_set)
        (local / "note.md").write_text("two")
        panel.observe(time.monotonic())
    finally:
        release.set()
    pump_until(app, lambda: not window.busy)
    assert "note.md" in panel.pending and (target / "note.md").read_text() == "one"
    panel.pending["note.md"] = 0
    with monkeypatch.context() as patch:
        patch.setattr(window, "_recovery_page_keys", lambda: ("transactions",))
        tick(app, window)
        assert "note.md" in panel.pending and "事务" in panel.summary
    tick(app, window)
    assert (target / "note.md").read_text() == "two"


def test_restart_and_reconnect_verify_original_root_and_target_edits(app, shell):
    original, local, target, _ = shell
    panel = enable(app, original, "mirror")
    tick(app, original)
    panel.close_watch()
    original._persist()
    fresh = MainWindow(original.state_dir)
    fresh._timer.stop()
    try:
        live = fresh.live_panels["mirror"]
        assert live.enabled and "" in live.pending
        moved = target.with_name("unplugged")
        target.rename(moved)
        tick(app, fresh)
        assert "等待移动硬盘" in live.summary
        (local / "note.md").write_text("my local edit")
        (moved / "note.md").write_text("portable edit")
        target.mkdir()
        live.next_probe = 0
        tick(app, fresh)
        assert not list(target.iterdir())
        target.rmdir()
        moved.rename(target)
        live.check_now()
        tick(app, fresh)
        assert (target / "note.md").read_text() == "portable edit" and "note.md" in live.issues
        assert (local / "note.md").read_text() == "my local edit"
    finally:
        dispose(app, fresh)


def test_leaving_during_initial_confirmation_and_direction_change(app, shell):
    window, local, target, _ = shell
    panel = window.live_panels["mirror"]
    panel.toggle.click()
    wait_for_confirmation(app, window)
    window._show_page("statistics")
    pump_until(app, lambda: not window.busy)
    assert not panel.enabled and not list(target.iterdir())
    enable(app, window, "mirror")
    window.set_direction("to_local")
    assert not panel.enabled
    window._timer_tick()
    assert not window.busy
