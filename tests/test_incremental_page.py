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


def test_deep_mode_is_explicit_and_invalidates_old_plan(application, prepared):
    window, page, local, portable = prepared
    assert not page.deep_checkbox.isChecked()
    page._start_scan()
    wait_until(application, lambda: not window.busy)
    assert not page.analysis.deep
    choose(page, "a", "portable")
    page.confirm_checkbox.setChecked(True)
    page.deep_checkbox.setChecked(True)
    assert page.analysis is None
    assert not page.confirm_checkbox.isChecked()
    page._start_scan()
    assert not page.deep_checkbox.isEnabled()
    wait_until(application, lambda: not window.busy)
    assert page.analysis.deep
    assert "完整校验" in page.summary_label.text()


@pytest.mark.parametrize("size", [(980, 620), (1140, 920), (2560, 1440)])
def test_stale_preflight_is_zero_write_and_full_diagnostics_are_logged(application, prepared, size):
    window, page, local, portable = prepared
    page._accept_analysis(IncrementalEngine(window.state_dir).analyze(str(local), str(portable)))
    choose(page, "a", "portable")
    (portable / "A/note.md").write_bytes(b"changed after preview")
    before = tree(local), tree(portable)
    messages = []
    page.message_logged.connect(messages.append)
    page.confirm_checkbox.setChecked(True)
    page.execute_button.click()
    wait_until(application, lambda: not window.busy)
    assert page.last_result.status == "failed" and not page.last_result.outcomes
    assert "本批尚未写入任何仓库" in page.status_label.text()
    assert "仓库 A" in page.status_label.toolTip()
    assert "note.md" in page.status_label.toolTip()
    assert "大小（字节）" in messages[-1]
    assert "note.md" in page.vault_table.item(0, 3).toolTip()
    assert messages[-1] in (window.state_dir / "ui.log").read_text(encoding="utf-8")
    assert (tree(local), tree(portable)) == before
    window.resize(*size)
    window.show()
    settle(application)
    assert window.page_containers["incremental"].horizontalScrollBar().maximum() == 0
    assert page.analysis is None and not page.execute_button.isEnabled()


def test_partial_execution_never_claims_zero_writes(application, prepared):
    from obmanage.management.incremental import IncrementalResult, VaultOutcome
    from obmanage.models import SyncResult
    window, page, local, portable = prepared
    page._accept_analysis(IncrementalEngine(window.state_dir).analyze(str(local), str(portable)))
    choose(page, "a", "portable")
    page._task_kind = "execute"
    page.task_finished("ok", IncrementalResult(
        status="failed", outcomes=[VaultOutcome("a", str(portable / "A"), str(local / "A"),
                                                SyncResult("failed", copied_files=1))],
        errors=["仓库 A：执行中断"],
    ))
    assert "执行中已停止，可能已有部分修改" in page.status_label.text()
    assert "尚未写入" not in page.status_label.toolTip()


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
