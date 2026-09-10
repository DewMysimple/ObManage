from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from obmanage.models import SyncCancelled
from obmanage.settings import SettingsStore
from obmanage.ui import FILTERS, MainWindow


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


def pump_until(app, condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    assert condition(), "The UI operation did not complete within the test timeout"


@pytest.fixture
def window(application, tmp_path):
    source, target = tmp_path / "源仓库", tmp_path / "目标镜像"
    source.mkdir()
    target.mkdir()
    widget = MainWindow(tmp_path / "settings")
    widget.source_edit.setText(str(source))
    widget.target_combo.setEditText(str(target))
    widget.show()
    application.processEvents()
    yield widget, source, target
    if widget.busy:
        widget.cancel_operation()
        pump_until(application, lambda: not widget.busy)
    widget._timer.stop()
    widget._save_timer.stop()
    widget.tray.hide()
    widget.hide()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_manual_preview_then_sync_and_repeat(application, window):
    widget, source, target = window
    (source / "新增笔记.md").write_text("今天的记录", encoding="utf-8")
    (source / "更新笔记.md").write_text("修订后的内容", encoding="utf-8")
    (target / "更新笔记.md").write_text("旧内容", encoding="utf-8")
    (source / "视频.mp4").write_bytes(b"unchanged video fixture")
    (target / "视频.mp4").write_bytes(b"unchanged video fixture")
    (target / "源端已经删除.md").write_text("待删除", encoding="utf-8")
    QTest.mouseClick(widget.analyze_button, Qt.MouseButton.LeftButton)
    assert widget.busy
    assert not widget.source_edit.isEnabled()
    assert not widget.target_combo.isEnabled()
    pump_until(application, lambda: not widget.busy)
    assert widget.plan.can_execute
    assert widget.plan.counts["delete"] == 1
    assert widget.plan.counts["skip"] == 1
    assert not widget.sync_button.isEnabled()
    widget.confirm_direction_checkbox.setChecked(True)
    assert widget.sync_button.isEnabled()
    widget.filter_tabs.setCurrentIndex(next(i for i, (_, actions) in enumerate(FILTERS)
                                           if actions == {"delete", "rmdir"}))
    assert widget.table.model().rowCount() == 1
    widget.filter_tabs.setCurrentIndex(0)
    preview = Path(__file__).resolve().parents[1] / "artifacts" / "ui-preview-test.png"
    preview.parent.mkdir(exist_ok=True)
    assert widget.grab().save(str(preview))
    QTest.mouseClick(widget.sync_button, Qt.MouseButton.LeftButton)
    pump_until(application, lambda: not widget.busy)
    assert widget.last_result.status == "success"
    assert widget.last_result.copied_files == 2
    assert not (target / "源端已经删除.md").exists()
    assert not widget.sync_button.isEnabled()
    widget.analyze()
    pump_until(application, lambda: not widget.busy)
    assert widget.plan.bytes_to_copy == 0
    assert not widget.plan.has_changes


def test_schedule_binding_target_change_and_tray(application, window, monkeypatch):
    widget, source, target = window
    widget.schedule_toggle.setChecked(True)
    assert widget.scheduler.next_run is not None
    assert widget.settings.bound_target == str(target)
    monkeypatch.setattr(widget.tray, "isVisible", lambda: True)
    widget.close()
    application.processEvents()
    assert not widget.isVisible()
    assert widget.schedule_toggle.isChecked()
    widget._show_window()
    widget.target_combo.setEditText(str(target.parent / "其他目标"))
    assert not widget.schedule_toggle.isChecked()
    assert widget.scheduler.next_run is None
    assert widget.plan is None
    widget._persist()
    assert not SettingsStore(widget.state_dir).load().schedule_enabled


def test_scheduled_tick_scans_then_executes_and_preserves_single_worker(application, window):
    widget, source, target = window
    (source / "定时.md").write_text("自动同步", encoding="utf-8")
    widget.schedule_toggle.setChecked(True)
    widget.scheduler.next_run = datetime.now() - timedelta(seconds=1)
    widget._timer_tick()
    first_thread = widget._thread
    assert widget.busy
    widget.analyze()
    assert widget._thread is first_thread
    pump_until(application, lambda: not widget.busy)
    assert widget.last_result.status == "success"
    assert (target / "定时.md").read_text(encoding="utf-8") == "自动同步"
    assert widget.scheduler.next_run > datetime.now()


def test_scheduled_empty_source_pauses_without_deleting(application, window):
    widget, source, target = window
    (target / "保留到手动确认.md").write_text("旧副本", encoding="utf-8")
    widget.schedule_toggle.setChecked(True)
    widget.analyze(scheduled=True)
    pump_until(application, lambda: not widget.busy)
    assert (target / "保留到手动确认.md").exists()
    assert not widget.schedule_toggle.isChecked()
    assert widget.plan.source_empty
    assert widget.last_result is None


def test_empty_source_manual_no_then_yes(application, window, monkeypatch):
    widget, source, target = window
    (target / "删除.md").write_text("old", encoding="utf-8")
    widget.analyze()
    pump_until(application, lambda: not widget.busy)
    widget.confirm_direction_checkbox.setChecked(True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    widget.start_sync()
    assert not widget.busy
    assert (target / "删除.md").exists()
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    widget.start_sync()
    pump_until(application, lambda: not widget.busy)
    assert widget.last_result.status == "success"
    assert not (target / "删除.md").exists()


def test_cancel_analysis_keeps_ui_responsive(application, window, monkeypatch):
    import obmanage.ui as ui

    widget, source, target = window

    def slow_analyze(self, *args, cancel=None, **kwargs):
        assert cancel is not None
        if cancel.wait(5):
            raise SyncCancelled("已取消")
        raise RuntimeError("Cancellation was not delivered")

    monkeypatch.setattr(ui.SyncEngine, "analyze", slow_analyze)
    widget.analyze()
    assert widget.busy
    assert widget.cancel_button.isEnabled()
    widget.cancel_operation()
    pump_until(application, lambda: not widget.busy)
    assert widget.plan is None
    assert widget.analyze_button.isEnabled()
    assert not widget.sync_button.isEnabled()


def test_offline_schedule_keeps_timer_for_next_attempt(application, window):
    widget, source, target = window
    source.rmdir()
    widget.schedule_toggle.setChecked(True)
    widget.analyze(scheduled=True)
    pump_until(application, lambda: not widget.busy)
    assert widget.plan.errors
    assert widget.schedule_toggle.isChecked()
    assert widget.scheduler.next_run is not None
    assert not widget.sync_button.isEnabled()
