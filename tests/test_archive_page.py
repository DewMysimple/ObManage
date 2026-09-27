import os
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QDateTime, QEvent
from PySide6.QtWidgets import QApplication

from obmanage.operation_log import LogEntry
from obmanage.pages.logs import OperationLogDialog
from obmanage.ui import MainWindow


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    widget = MainWindow(tmp_path / "state")
    yield app, widget
    if widget.busy:
        widget.cancel_operation()
        wait(app, lambda: not widget.busy)
    widget._timer.stop()
    widget._save_timer.stop()
    widget.tray.hide()
    widget.hide()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def wait(app, condition):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.01)
    assert condition()


def test_archive_worker_end_to_end_and_context_survives_navigation(window, tmp_path, monkeypatch):
    app, widget = window
    source = tmp_path / "vault"
    (source / ".obsidian").mkdir(parents=True)
    (source / "note.md").write_text("note", encoding="utf-8")
    (source / "video.mp4").write_bytes(b"video")
    output = tmp_path / "sync folder"
    output.mkdir()
    opened = []
    monkeypatch.setattr("obmanage.pages.archive.open_baidu", lambda path: opened.append(path) or "已请求打开网盘")
    page = widget.pages["archive"]
    widget._show_page("archive")
    page.source_picker.set_value(str(source))
    page.destination_mode.setCurrentIndex(1)
    page.output_edit.setText(str(output))
    page.exclude_videos.setChecked(True)
    page.launch_baidu.setChecked(True)
    page.client_edit.setText("C:/Demo/BaiduNetdisk.exe")
    page.analyze()
    assert widget.busy and not widget.analyze_button.isEnabled()
    wait(app, lambda: not widget.busy)
    assert page.plan is not None and page.execute_button.isEnabled()
    page.execute()
    widget._show_page("statistics")
    wait(app, lambda: not widget.busy)
    assert page.result is not None
    assert opened == ["C:/Demo/BaiduNetdisk.exe"]
    with zipfile.ZipFile(page.result.output) as archive:
        assert archive.read("note.md") == b"note"
        assert "video.mp4" not in archive.namelist()
    rows = [row for row in widget._log_entries if row.task]
    assert rows and all(row.feature == "archive" for row in rows)
    assert len({row.task for row in rows}) == 2
    assert any(row.level == "success" and row.event == "打包" for row in rows)
    assert page.settings_payload()["baidu_dir"] == str(output)
    page.remember.setChecked(False)
    assert page.settings_payload()["baidu_dir"] == ""
    page._external_recovery_pending = True
    page.refresh_actions()
    assert not page.execute_button.isEnabled()


def test_controls_invalidate_preview_and_profiles_remember_separate_locations(window, tmp_path):
    app, widget = window
    page = widget.pages["archive"]
    page.output_edit.setText(str(tmp_path))
    page.destination_mode.setCurrentIndex(1)
    assert page.output_edit.text() == ""
    page.output_edit.setText(str(tmp_path / "cloud"))
    page.destination_mode.setCurrentIndex(0)
    assert page.output_edit.text() == str(tmp_path)
    page.preset.setCurrentIndex(page.preset.findData(9))
    assert page.level.value() == 9
    page.level.setValue(4)
    assert page.preset.currentData() == -1
    page.plan = object()
    page.exclude_videos.setChecked(True)
    assert page.plan is None
    page.set_global_busy(True)
    assert not page.level.isEnabled() and not page.source_picker.vault_list.isEnabled()
    assert widget.navigation_buttons["templater"].text() == ".Templater"
    assert widget.navigation_buttons["trash_cleanup"].text() == ".Trash"


def test_log_filters_persist_as_new_rows_arrive_and_time_range_is_inclusive(window):
    app, widget = window
    rows = [LogEntry("2026-09-20 10:00:00", "before", "archive", "打包", "one"),
            LogEntry("2026-09-21 10:00:00", "selected", "archive", "打包", "two"),
            LogEntry("2026-09-21 10:00:00", "other", "mirror", "执行", "three")]
    dialog = OperationLogDialog(widget)
    dialog.set_entries(rows)
    dialog.feature.setCurrentIndex(dialog.feature.findData("archive"))
    dialog.task.setCurrentIndex(dialog.task.findData("two"))
    dialog.period.setCurrentIndex(dialog.period.findData("custom"))
    stamp = QDateTime.fromString("2026-09-21 10:00:00", "yyyy-MM-dd HH:mm:ss")
    dialog.start.setDateTime(stamp)
    dialog.end.setDateTime(stamp)
    assert dialog.filtered == [rows[1]]
    dialog.set_entries(rows + [LogEntry("2026-09-22 10:00:00", "new", "mirror")])
    assert dialog.task.currentData() == "two" and dialog.filtered == [rows[1]]
    dialog.start.setDateTime(stamp.addSecs(1))
    assert not dialog.filtered and "不能晚于" in dialog.summary.text()
    dialog.reset_filters()
    assert len(dialog.filtered) == 4
    dialog.close()


@pytest.mark.parametrize("size", [(980, 620), (1140, 920), (2560, 1440)])
def test_cloud_archive_and_log_filters_fit_small_normal_and_large(window, size):
    from PySide6.QtCore import QPoint, QRect
    app, widget = window
    page = widget.pages["archive"]
    page.destination_mode.setCurrentIndex(1)
    page.launch_baidu.setChecked(True)
    widget._show_page("archive")
    widget.resize(*size)
    widget.show()
    for _ in range(5):
        app.processEvents()
    scroll = widget.page_containers["archive"]
    assert scroll.horizontalScrollBar().maximum() == 0
    assert page.source_picker.edit.width() >= 160
    assert page.table.horizontalHeader().length() == page.table.viewport().width()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    app.processEvents()
    assert scroll.viewport().rect().contains(QRect(page.execute_button.mapTo(scroll.viewport(), QPoint()), page.execute_button.size()))
    widget.show_logs()
    dialog = widget._log_dialog
    dialog.resize(min(size[0], 980), min(size[1], 640))
    dialog.period.setCurrentIndex(dialog.period.findData("custom"))
    app.processEvents()
    for control in (dialog.period, dialog.feature, dialog.event, dialog.task, dialog.start, dialog.end, dialog.search):
        assert dialog.rect().contains(QRect(control.mapTo(dialog, QPoint()), control.size()))


def test_due_mirror_timer_is_not_logged_as_active_archive_task(window, tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from threading import Event
    app, widget = window
    source = tmp_path / "vault"
    (source / ".obsidian").mkdir(parents=True)
    page = widget.pages["archive"]
    page.source_picker.set_value(str(source))
    page.output_edit.setText(str(tmp_path))
    entered, release = Event(), Event()
    original = page.engine.analyze
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(page.engine, "analyze", delayed)
    page.analyze()
    try:
        wait(app, entered.is_set)
        widget.settings.schedule_enabled = True
        widget.settings.bound_source = widget.settings.source
        widget.settings.bound_target = widget.settings.target
        widget.scheduler.configure(widget.settings)
        widget.scheduler.next_run = datetime.now() - timedelta(seconds=1)
        widget._timer_tick()
        record = next(e for e in reversed(widget._log_entries) if e.event == "定时跳过")
        assert record.feature == "mirror" and record.task == "" and record.level == "warning"
        assert widget._log_context[0] == "archive"
    finally:
        release.set()
        wait(app, lambda: not widget.busy)


def test_partial_task_result_is_warning_in_log(window):
    from obmanage.management.models import ManagementIssue, VaultCatalogResult
    app, widget = window
    widget._begin_log_task("statistics", "statistics")
    context = widget._log_context
    widget._end_log_task(context, ("ok", VaultCatalogResult(issues=(ManagementIssue("read", "unreadable"),))))
    assert widget._log_entries[-1].level == "warning"
