from __future__ import annotations

import ntpath
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QMenu

from obmanage.models import PlanItem, SyncPlan
from obmanage.ui import FILTERS, MainWindow


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


def settle(app):
    """Deliver resize/layout/paint events without starving Python workers."""
    deadline = time.monotonic() + 0.06
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def preview_window(application, tmp_path):
    # Synthetic roots make these layout tests independent of any attached disk.
    # No scan or execution is started: only the plan presenter is exercised.
    widget = MainWindow(tmp_path / "state")
    widget.local_edit.setText(r"C:\本机资料\Obsidian仓库")
    widget.portable_combo.setEditText(r"H:\移动工作资料\Obsidian仓库")
    widget.set_direction("to_local")
    long_folder = "\\".join(["长期课程资料与项目笔记" * 2] * 14)
    actions = ("add", "update", "rename", "delete", "mkdir", "rmdir", "skip")
    items = [
        PlanItem(action, f"{long_folder}\\文件{index:03d}-{action}.md", 128 * 1024, "用于预览的差异")
        for index in range(35)
        for action in actions
    ]
    source, target = widget._paths()
    widget._accept_plan(SyncPlan(source, target, items), scheduled=False)
    widget.show()
    settle(application)
    yield widget
    assert not widget.busy, "A layout test must never start a filesystem operation"
    widget._timer.stop()
    widget._save_timer.stop()
    widget.tray.hide()
    widget.hide()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def column(model, title):
    headers = [model.headerData(index, Qt.Orientation.Horizontal) for index in range(model.columnCount())]
    return headers.index(title)


def select_filter(widget, actions):
    widget.filter_tabs.setCurrentIndex(next(index for index, (_, value) in enumerate(FILTERS) if value == actions))


def assert_table_fills_width(widget):
    table = widget.table
    header = table.horizontalHeader()
    available = table.viewport().width()
    last = header.count() - 1
    right_edge = header.sectionViewportPosition(last) + header.sectionSize(last)
    assert abs(header.length() - available) <= 1, "Columns must occupy the table viewport without a trailing gap"
    assert abs(right_edge - available) <= 1, "The last column must remain visible at the right viewport edge"
    assert table.horizontalScrollBar().maximum() == 0, "Reading the final column must not require horizontal scrolling"
    assert not table.horizontalScrollBar().isVisible()


@pytest.mark.parametrize("size", [(1140, 920), (980, 720), (2560, 1440)])
def test_columns_fill_regular_laptop_and_full_screen_windows(application, preview_window, size):
    widget = preview_window
    # Returning through another size catches widths cached from the first layout.
    for width, height in (size, (1040, 720), size):
        widget.resize(width, height)
        for actions in (None, {"delete", "rmdir"}, {"skip"}, {"add", "mkdir"}):
            select_filter(widget, actions)
            settle(application)
            assert widget.table.isVisible()
            assert_table_fills_width(widget)
    # A category without rows switches to the empty state; returning must refit
    # columns, including the appearance/disappearance of a vertical scrollbar.
    select_filter(widget, {"error"})
    settle(application)
    select_filter(widget, None)
    settle(application)
    assert_table_fills_width(widget)


@pytest.mark.parametrize("size", [(980, 720), (1140, 920), (2560, 1440)])
def test_preview_has_priority_and_primary_actions_stay_visible(application, preview_window, size):
    widget = preview_window
    widget.resize(*size)
    settle(application)
    assert widget.preview_title.mapTo(widget, QPoint()).y() < widget.height() / 2, "The preview should start in the upper half of the window"
    assert widget.table.viewport().height() >= 4 * widget.table.rowHeight(0), "At least four full file rows should fit even on a laptop"
    assert widget.table.height() > widget.paths_panel.height(), "Path controls should not dominate the file preview"
    for control in (widget.analyze_button, widget.sync_button, widget.confirm_direction_checkbox):
        assert control.isVisible()
        assert widget.rect().contains(control.mapTo(widget, QPoint()))
        assert widget.rect().contains(control.mapTo(widget, control.rect().bottomRight()))


def test_four_columns_keep_deletion_target_and_read_only_source_clear(preview_window):
    widget = preview_window
    select_filter(widget, {"delete", "rmdir"})
    model = widget.table.model()
    headers = [model.headerData(index, Qt.Orientation.Horizontal) for index in range(model.columnCount())]
    assert headers == ["操作", "实际修改位置", "大小", "说明"]
    source, target = widget._paths()
    action = model.index(0, column(model, "操作")).data()
    target_index = model.index(0, column(model, "实际修改位置"))
    target_path = target_index.data()
    tooltip = target_index.data(Qt.ItemDataRole.ToolTipRole)
    assert "删除" in action and "本机" in action and "C:" in action
    assert "H:" not in action and "移动硬盘" not in action
    assert ntpath.commonpath([target_path, target]) == target
    assert "不存在" in model.index(0, column(model, "说明")).data()
    assert target_path in tooltip and source in tooltip
    assert "只读" in tooltip or "只读取" in tooltip
    assert "只读" in widget.source_safety_label.text() or "只读取" in widget.source_safety_label.text()
    assert "移动硬盘" in widget.direction_label.text() and "H:" in widget.direction_label.text()
    assert "本机" in widget.direction_label.text() and "C:" in widget.direction_label.text()
    assert widget.direction_label.text().index("H:") < widget.direction_label.text().index("C:")
    assert source in widget.source_safety_label.toolTip()
    assert target in widget.target_effect_label.toolTip()
    assert "删除" in widget.target_effect_label.text()
    # Paths stay available in their input fields; safety text need not repeat
    # entire roots in multiple cards above the table.
    assert source not in widget.source_safety_label.text()
    assert target not in widget.target_effect_label.text()


def test_long_filtered_path_tooltip_and_copy_remain_complete(application, preview_window, monkeypatch):
    widget = preview_window
    widget.resize(980, 720)
    select_filter(widget, {"delete", "rmdir"})
    settle(application)
    model = widget.table.model()
    path_index = model.index(3, column(model, "实际修改位置"))
    full_path = path_index.data()
    assert len(full_path) > 260
    assert widget.table.fontMetrics().horizontalAdvance(full_path) > widget.table.columnWidth(path_index.column())
    assert full_path in path_index.data(Qt.ItemDataRole.ToolTipRole)
    widget.table.selectRow(path_index.row())

    class ChooseTargetMenu(QMenu):
        def exec(self, *args, **kwargs):
            return next(action for action in self.actions() if action.text() == "复制实际修改位置")

    # PySide's native QMenu.exec can bypass a monkeypatch on the extension class;
    # substituting a subclass avoids entering a blocking native popup loop.
    monkeypatch.setattr("obmanage.ui.QMenu", ChooseTargetMenu)
    QApplication.clipboard().clear()
    widget._table_context_menu(QPoint(10, 10))
    assert QApplication.clipboard().text() == full_path
    assert_table_fills_width(widget)


def test_rendered_cells_have_visible_column_separators(application, preview_window):
    widget = preview_window
    widget.resize(1140, 920)
    select_filter(widget, {"delete", "rmdir"})
    settle(application)
    table = widget.table
    table.clearSelection()
    picture = table.viewport().grab().toImage()
    scale = picture.devicePixelRatio()

    def color(x, y):
        return picture.pixelColor(round(x * scale), round(y * scale)).rgba()

    header = table.horizontalHeader()
    # Sample the blank lower part of several rows so text/ellipsis cannot be
    # mistaken for a separator. This checks rendered contrast, whether the UI
    # uses a grid, item borders or a custom painter.
    for section in range(header.count() - 1):
        boundary = header.sectionViewportPosition(section + 1)
        for row in range(3):
            y = table.rowViewportPosition(row) + table.rowHeight(row) - 5
            interior_left, interior_right = color(boundary - 6, y), color(boundary + 6, y)
            boundary_colors = [color(boundary + offset, y) for offset in (-1, 0, 1)]
            assert any(value != interior_left and value != interior_right for value in boundary_colors), (
                f"No visible divider between columns {section} and {section + 1} in row {row}"
            )


@pytest.mark.parametrize("size", [(980, 620), (1140, 920), (2560, 1440)])
def test_every_management_page_fits_width_and_bottom_actions_are_reachable(
    application, tmp_path, size
):
    window = MainWindow(tmp_path / f"state-{size[0]}-{size[1]}")
    window.resize(*size)
    window.show()
    try:
        bottom_controls = {
            "statistics": (window.pages["statistics"].scan_button,),
            "template_suite": (
                window.pages["template_suite"].rollback_button,
                window.pages["template_suite"].execute_button,
            ),
            "obsidian_config": (
                window.pages["obsidian_config"].rollback_button,
                window.pages["obsidian_config"].execute_button,
            ),
            "templater": (
                window.pages["templater"].rollback_button,
                window.pages["templater"].execute_button,
            ),
            "trash_cleanup": (window.pages["trash_cleanup"].clear_button,),
        }
        for key, controls in bottom_controls.items():
            window._show_page(key, persist=False)
            scroll = window.page_containers[key]
            settle(application)
            assert scroll.horizontalScrollBar().maximum() == 0, (
                f"{key} must not require horizontal scrolling at {size}"
            )
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            settle(application)
            viewport = scroll.viewport()
            for control in controls:
                origin = control.mapTo(viewport, QPoint(0, 0))
                mapped = QRect(origin, control.size())
                assert viewport.rect().intersects(mapped), (
                    f"{key} bottom action {control.objectName() or control.text()} "
                    f"is unreachable at {size}"
                )
        if size == (980, 620):
            for key in ("template_suite", "obsidian_config", "templater"):
                assert window.page_containers[key].verticalScrollBar().maximum() > 0
    finally:
        assert not window.busy
        window._timer.stop()
        window._save_timer.stop()
        window.tray.hide()
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_legacy_trash_recovery_actions_fit_and_are_scroll_reachable(
    application, tmp_path, seed_legacy_quarantine
):
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash" / "restore.md").write_text("restore", encoding="utf-8")
    seed_legacy_quarantine(state, vault)
    window = MainWindow(state)
    window.resize(980, 620)
    window.show()
    try:
        window._show_page("trash_cleanup", persist=False)
        scroll = window.page_containers["trash_cleanup"]
        page = window.pages["trash_cleanup"]
        settle(application)
        assert not page.legacy_recovery_panel.isHidden()
        assert scroll.horizontalScrollBar().maximum() == 0
        assert scroll.verticalScrollBar().maximum() > 0

        for control in (page.restore_button, page.finalize_button):
            scroll.ensureWidgetVisible(control)
            settle(application)
            viewport = scroll.viewport()
            origin = control.mapTo(viewport, QPoint(0, 0))
            assert viewport.rect().intersects(QRect(origin, control.size()))
    finally:
        assert not window.busy
        window._timer.stop()
        window._save_timer.stop()
        window.tray.hide()
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
