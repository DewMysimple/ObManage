from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from obmanage.models import SyncCancelled
from obmanage.settings import SettingsStore
from obmanage.ui import MainWindow, PlanTableModel


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


def wait_for_idle(app, widget, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if not widget.busy:
            return
        # Let the Python worker run as well as dispatching Qt events.
        time.sleep(0.01)
    assert not widget.busy, "The direction workflow did not finish in time"


def dispose_window(app, widget):
    if widget.busy:
        widget.cancel_operation()
        wait_for_idle(app, widget)
    widget._timer.stop()
    widget._save_timer.stop()
    widget.tray.hide()
    widget.hide()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def directional_window(application, tmp_path):
    local, portable = tmp_path / "本机", tmp_path / "移动硬盘"
    local.mkdir()
    portable.mkdir()
    widget = MainWindow(tmp_path / "settings")
    widget.local_edit.setText(str(local))
    widget.portable_combo.setEditText(str(portable))
    widget.set_direction("to_portable")
    widget.show()
    application.processEvents()
    yield widget, local, portable
    dispose_window(application, widget)


def snapshot(root: Path):
    """Include source directory mtimes to catch unexpected source-side writes."""
    paths = [root, *sorted(root.rglob("*"))]
    return {
        path.relative_to(root).as_posix(): (
            path.is_dir(),
            path.stat().st_mtime_ns,
            None if path.is_dir() else path.read_bytes(),
        )
        for path in paths
    }


def contents(root: Path):
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


def analyze_and_confirm(app, widget):
    widget.analyze()
    wait_for_idle(app, widget)
    assert widget.plan is not None
    assert widget.plan.can_execute, widget.plan.errors
    assert widget.plan.has_changes
    assert not widget.confirm_direction_checkbox.isChecked()
    assert not widget.sync_button.isEnabled()
    widget.confirm_direction_checkbox.setChecked(True)
    assert widget.sync_button.isEnabled()


def test_direction_buttons_keep_endpoint_rows_and_name_target(directional_window):
    widget, local, portable = directional_window
    assert widget._paths() == (str(local), str(portable))
    QTest.mouseClick(widget.direction_to_local_button, Qt.MouseButton.LeftButton)
    assert widget.settings.direction == "to_local"
    assert widget.local_edit.text() == str(local)
    assert widget.portable_combo.currentText() == str(portable)
    assert widget._paths() == (str(portable), str(local))
    assert "移动硬盘" in widget.direction_label.text()
    assert "本机" in widget.direction_label.text()
    assert widget.direction_label.text().index("移动硬盘") < widget.direction_label.text().index("本机")
    assert str(portable) in widget.source_safety_label.toolTip()
    assert "只读" in widget.source_safety_label.text()
    assert str(local) in widget.target_effect_label.toolTip()
    assert "删除" in widget.target_effect_label.text()
    assert "本机" in widget.preview_title.text()
    assert "本机" in widget.stat_titles["delete"].toolTip()
    QTest.mouseClick(widget.direction_to_portable_button, Qt.MouseButton.LeftButton)
    assert widget.settings.direction == "to_portable"
    assert widget._paths() == (str(local), str(portable))
    assert widget.local_edit.text() == str(local)
    assert widget.portable_combo.currentText() == str(portable)
    assert "移动硬盘" in widget.preview_title.text()
    assert "移动硬盘" in widget.stat_titles["delete"].toolTip()


def test_switch_discards_preview_confirmation_and_schedule(application, directional_window):
    widget, local, portable = directional_window
    (local / "笔记.md").write_text("本机修改", encoding="utf-8")
    (portable / "笔记.md").write_text("移动硬盘修改", encoding="utf-8")
    analyze_and_confirm(application, widget)
    widget.schedule_toggle.setChecked(True)
    assert widget.scheduler.next_run is not None
    widget.set_direction("to_local")
    assert widget.plan is None
    assert widget.table_model.rowCount() == 0
    assert not widget.confirm_direction_checkbox.isChecked()
    assert not widget.sync_button.isEnabled()
    assert not widget.schedule_toggle.isChecked()
    assert widget.scheduler.next_run is None
    widget.start_sync()
    assert not widget.busy
    assert widget.last_result is None
    widget._persist()
    reloaded = SettingsStore(widget.state_dir).load()
    assert reloaded.direction == "to_local"
    assert not reloaded.schedule_enabled
    analyze_and_confirm(application, widget)
    assert widget._normalized_path(widget.plan.source) == widget._normalized_path(str(portable))
    assert widget._normalized_path(widget.plan.target) == widget._normalized_path(str(local))


def test_complete_ui_roundtrip_preserves_each_source(application, directional_window):
    widget, local, portable = directional_window
    (local / ".obsidian").mkdir()
    (local / ".obsidian" / "app.json").write_text('{"theme":"dark"}', encoding="utf-8")
    (local / "视频.mp4").write_bytes(b"unchanged-video-content" * 100)
    (local / "笔记.md").write_text("主机上的第一版", encoding="utf-8")
    (local / "随后删除.md").write_text("笔记本上将删除", encoding="utf-8")
    (local / "随后删除的文件夹").mkdir()
    (local / "随后删除的文件夹" / "旧.md").write_text("旧", encoding="utf-8")
    initial_local = snapshot(local)
    analyze_and_confirm(application, widget)
    QTest.mouseClick(widget.sync_button, Qt.MouseButton.LeftButton)
    wait_for_idle(application, widget)
    assert widget.last_result.status == "success"
    assert snapshot(local) == initial_local
    assert contents(local) == contents(portable)

    # Simulate work performed on the laptop with the portable vault attached.
    (portable / "笔记.md").write_text("笔记本完成的修订版本与新增内容", encoding="utf-8")
    (portable / ".obsidian" / "app.json").write_text('{"theme":"light"}', encoding="utf-8")
    (portable / "随后删除.md").unlink()
    (portable / "随后删除的文件夹" / "旧.md").unlink()
    (portable / "随后删除的文件夹").rmdir()
    (portable / "笔记本新增.md").write_text("带回主机的新笔记", encoding="utf-8")
    (portable / "笔记本新建空目录").mkdir()
    portable_before_pull = snapshot(portable)
    unchanged_video_mtime = (local / "视频.mp4").stat().st_mtime_ns
    widget.set_direction("to_local")
    analyze_and_confirm(application, widget)
    assert widget.plan.counts["delete"] == 2
    assert widget.plan.counts["rmdir"] == 1
    assert widget.plan.counts["skip"] == 1
    assert snapshot(portable) == portable_before_pull
    QTest.mouseClick(widget.sync_button, Qt.MouseButton.LeftButton)
    wait_for_idle(application, widget)
    assert widget.last_result.status == "success", widget.last_result.errors
    assert snapshot(portable) == portable_before_pull
    assert contents(local) == contents(portable)
    assert not (local / "随后删除.md").exists()
    assert not (local / "随后删除的文件夹").exists()
    assert (local / "视频.mp4").stat().st_mtime_ns == unchanged_video_mtime
    widget.analyze()
    wait_for_idle(application, widget)
    assert widget.plan.bytes_to_copy == 0
    assert not widget.plan.has_changes
    assert snapshot(portable) == portable_before_pull


def test_pull_preview_names_delete_location_and_hides_skips(application, directional_window):
    widget, local, portable = directional_window
    for index in range(75):
        name = f"旧视频{index:03d}.mp4"
        (local / name).write_bytes(b"stable content")
        (portable / name).write_bytes(b"stable content")
    delete_path = local / "在笔记本上已删除.md"
    delete_path.write_text("本机的过期副本", encoding="utf-8")
    before = snapshot(portable)
    widget.set_direction("to_local")
    widget.analyze()
    wait_for_idle(application, widget)
    assert widget.plan.counts["skip"] == 75
    assert widget.plan.counts["delete"] == 1
    model = widget.table.model()
    assert model.rowCount() == 1
    row = "\n".join(str(model.data(model.index(0, col)) or "") for col in range(model.columnCount()))
    assert "删除" in row
    assert "本机" in row
    assert "移动硬盘" in model.index(0, PlanTableModel.TARGET_COLUMN).data(Qt.ItemDataRole.ToolTipRole)
    assert "不存在" in row
    assert widget._normalized_path(str(delete_path)) == widget._normalized_path(str(model.data(model.index(0, PlanTableModel.TARGET_COLUMN))))
    assert "本机" in str(model.data(model.index(0, 0)))
    assert "移动硬盘" not in str(model.data(model.index(0, 0)))
    assert snapshot(portable) == before
    # All rows remain available explicitly without overwhelming the initial preview.
    all_tab = next(index for index in range(widget.filter_tabs.count()) if widget.filter_tabs.tabText(index).startswith("全部"))
    widget.filter_tabs.setCurrentIndex(all_tab)
    assert model.rowCount() == 76


@pytest.mark.parametrize("changed_endpoint", ["local", "portable"])
def test_endpoint_edit_revokes_confirmation(application, directional_window, changed_endpoint):
    widget, local, portable = directional_window
    (portable / "笔记.md").write_text("移动硬盘最新内容", encoding="utf-8")
    widget.set_direction("to_local")
    analyze_and_confirm(application, widget)
    if changed_endpoint == "local":
        widget.local_edit.setText(str(local.parent / "另一台本机"))
    else:
        widget.portable_combo.setEditText(str(portable.parent / "另一块移动硬盘"))
    assert widget.plan is None
    assert not widget.confirm_direction_checkbox.isChecked()
    assert not widget.sync_button.isEnabled()
    widget.start_sync()
    assert not widget.busy
    assert list(local.iterdir()) == []


def test_manual_execution_requires_acknowledgment_even_when_called_directly(application, directional_window):
    widget, local, portable = directional_window
    (portable / "最新.md").write_text("便携来源", encoding="utf-8")
    (local / "应该删除.md").write_text("旧的本机副本", encoding="utf-8")
    before_local, before_portable = snapshot(local), snapshot(portable)
    widget.set_direction("to_local")
    widget.analyze()
    wait_for_idle(application, widget)
    assert not widget.sync_button.isEnabled()
    widget.start_sync()
    assert not widget.busy
    assert widget.last_result is None
    assert snapshot(local) == before_local
    assert snapshot(portable) == before_portable
    widget.confirm_direction_checkbox.setChecked(True)
    assert widget.sync_button.isEnabled()
    widget.analyze()
    wait_for_idle(application, widget)
    assert not widget.confirm_direction_checkbox.isChecked()
    assert not widget.sync_button.isEnabled()


def test_busy_operation_cannot_change_direction(application, directional_window, monkeypatch):
    import obmanage.ui as ui

    widget, local, portable = directional_window
    widget.set_direction("to_local")

    def slow_analyze(self, *args, cancel=None, **kwargs):
        assert cancel is not None
        if cancel.wait(5):
            raise SyncCancelled("已取消")
        raise RuntimeError("Cancellation was not delivered")

    monkeypatch.setattr(ui.SyncEngine, "analyze", slow_analyze)
    widget.analyze()
    assert widget.busy
    assert not widget.direction_to_local_button.isEnabled()
    assert not widget.direction_to_portable_button.isEnabled()
    assert not widget.local_edit.isEnabled()
    assert not widget.portable_combo.isEnabled()
    QTest.mouseClick(widget.direction_to_portable_button, Qt.MouseButton.LeftButton)
    widget.set_direction("to_portable")
    assert widget.settings.direction == "to_local"
    assert widget._paths() == (str(portable), str(local))
    widget.cancel_operation()
    wait_for_idle(application, widget)
    assert widget.direction_to_portable_button.isEnabled()


def test_scheduled_pull_writes_only_local_and_switch_pauses(application, directional_window):
    widget, local, portable = directional_window
    (portable / "带回主机.md").write_text("笔记本的新修改", encoding="utf-8")
    (local / "笔记本上已删除.md").write_text("本机旧版本", encoding="utf-8")
    portable_before = snapshot(portable)
    widget.set_direction("to_local")
    widget.schedule_toggle.setChecked(True)
    assert widget.settings.bound_source == str(portable)
    assert widget.settings.bound_target == str(local)
    assert widget.settings.direction == "to_local"
    widget.scheduler.next_run = datetime.now() - timedelta(seconds=1)
    widget._timer_tick()
    assert widget.busy
    first_thread = widget._thread
    widget._timer_tick()
    assert widget._thread is first_thread
    wait_for_idle(application, widget)
    assert widget.last_result.status == "success"
    assert contents(local) == contents(portable)
    assert snapshot(portable) == portable_before
    assert not (local / "笔记本上已删除.md").exists()
    widget.set_direction("to_portable")
    assert not widget.schedule_toggle.isChecked()
    assert widget.scheduler.next_run is None
    widget._timer_tick()
    assert not widget.busy
    assert snapshot(portable) == portable_before


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_windows_config_opens_in_preserved_direction(application, tmp_path, reverse):
    local = r"C:\Users\Administrator\Desktop\Obsidian仓库"
    portable = r"H:\ObsidianVault\Obsidian仓库"
    source, target = (portable, local) if reverse else (local, portable)
    state_dir = tmp_path / "settings"
    state_dir.mkdir()
    (state_dir / "settings.json").write_text(
        json.dumps({"source": source, "target": target, "schedule_enabled": True,
                    "bound_source": source, "bound_target": target}, ensure_ascii=False),
        encoding="utf-8",
    )
    widget = MainWindow(state_dir)
    try:
        assert widget.local_edit.text() == local
        assert widget.portable_combo.currentText() == portable
        assert widget._paths() == (source, target)
        assert widget.settings.direction == ("to_local" if reverse else "to_portable")
        assert not widget.schedule_toggle.isChecked()
        assert widget.scheduler.next_run is None
        assert not widget.confirm_direction_checkbox.isChecked()
        assert not widget.sync_button.isEnabled()
    finally:
        dispose_window(application, widget)
