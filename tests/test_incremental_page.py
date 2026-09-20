from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication

from obmanage.management.incremental import IncrementalEngine
from obmanage.settings import SettingsStore
from obmanage.ui import MainWindow
from test_backup_page import dispose, settle, wait_until
from test_incremental import tree, vault


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def prepared(application, tmp_path):
    local, portable = tmp_path / "local", tmp_path / "portable"
    for root in (local, portable):
        for name in ("A", "B", "same"):
            vault(root, name, {"note.md": b"same"})
    (portable / "A/note.md").write_bytes(b"new A")
    (local / "B/note.md").write_bytes(b"new B")
    window = MainWindow(tmp_path / "state")
    page = window.pages["incremental"]
    page.local_picker.set_value(str(local))
    page.portable_picker.set_value(str(portable))
    window._show_page("incremental")
    yield window, page, local, portable
    dispose(application, window)


def choose(page, key, side):
    selector = page.source_selectors[key]
    selector.setCurrentIndex(selector.findData(side))


def test_real_worker_mixed_directions_confirmation_and_settings(application, prepared):
    window, page, local, portable = prepared
    before = tree(local), tree(portable)
    page._start_scan()
    assert window.busy
    assert not window.analyze_button.isEnabled()
    wait_until(application, lambda: not window.busy)
    assert (tree(local), tree(portable)) == before
    assert page.vault_table.rowCount() == 2
    assert page.choices == {}
    assert all(selector.currentData() == "" for selector in page.source_selectors.values())
    assert not page.execute_button.isEnabled()
    choose(page, "a", "portable")
    assert "移动硬盘 → 电脑" in page.preview_title.text()
    assert str(local / "A") in page.model.target
    assert str(local / "A") in page.model.index(0, 1).data(Qt.ItemDataRole.ToolTipRole)
    page.confirm_checkbox.setChecked(True)
    choose(page, "b", "local")
    assert not page.confirm_checkbox.isChecked()
    assert "电脑 → 移动硬盘" in page.preview_title.text()
    page.confirm_checkbox.setChecked(True)
    window._show_page("statistics")
    assert not page.confirm_checkbox.isChecked()
    window._show_page("incremental")
    page.confirm_checkbox.setChecked(True)
    page.execute_button.click()
    wait_until(application, lambda: not window.busy)
    assert page.last_result.status == "success"
    assert tree(local) == tree(portable)
    assert page.analysis is None
    assert not page.execute_button.isEnabled()
    assert all("完成" in page.vault_table.item(row, 3).text() for row in (0, 1))
    assert window._persist()
    settings = SettingsStore(window.state_dir).load_document().features["incremental"]
    assert settings == {"local_path": str(local), "portable_path": str(portable)}
    page._start_scan()
    wait_until(application, lambda: not window.busy)
    assert page.vault_table.rowCount() == 0
    assert page.analysis.identical_count == 3


def test_path_changes_and_recovery_gate_revoke_execution(application, prepared):
    window, page, local, portable = prepared
    analysis = IncrementalEngine(window.state_dir).analyze(str(local), str(portable))
    page._accept_analysis(analysis)
    choose(page, "a", "portable")
    page.confirm_checkbox.setChecked(True)
    page.set_external_recovery_pending(True)
    assert not page.execute_button.isEnabled()
    page._start_execute()
    assert not window.busy
    page.set_external_recovery_pending(False)
    page.local_picker.set_value(str(local / "changed"))
    assert page.analysis is None
    assert page.choices == {}
    assert not page.confirm_checkbox.isChecked()
    assert not page.execute_button.isEnabled()


@pytest.mark.parametrize("size", [(980, 620), (1140, 920), (2560, 1440)])
def test_populated_tables_and_bottom_actions_fit(application, prepared, size):
    window, page, local, portable = prepared
    analysis = IncrementalEngine(window.state_dir).analyze(str(local), str(portable))
    page._accept_analysis(analysis)
    choose(page, "a", "portable")
    window.resize(*size)
    window.show()
    settle(application)
    scroll = window.page_containers["incremental"]
    assert scroll.horizontalScrollBar().maximum() == 0
    for table in (page.vault_table, page.table):
        assert table.showGrid()
        assert table.horizontalScrollBar().maximum() == 0
        assert abs(table.horizontalHeader().length() - table.viewport().width()) <= 1
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    settle(application)
    for control in (page.confirm_checkbox, page.execute_button):
        mapped = QRect(control.mapTo(scroll.viewport(), QPoint(0, 0)), control.size())
        assert scroll.viewport().rect().intersects(mapped)
