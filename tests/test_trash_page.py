from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from obmanage.management.trash import TrashCleanupEngine, TrashProgress
from obmanage.pages.trash_cleanup import TrashCleanupPage


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


def dispose(page) -> None:
    page.hide()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def make_vault(root: Path, name: str) -> Path:
    vault = root / name
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    return vault


def run_last(page, operations: list, starter):
    before = len(operations)
    starter()
    assert len(operations) == before + 1
    result = operations[-1](threading.Event(), lambda _event: None)
    page.task_finished("ok", result)
    return result


def check_vault(page: TrashCleanupPage, row: int = 0, checked: bool = True) -> None:
    index = page.vault_model.index(row, 0)
    assert page.vault_model.setData(
        index,
        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
        Qt.ItemDataRole.CheckStateRole,
    )


def create_pending_trash_operation(state: Path, vault: Path):
    engine = TrashCleanupEngine(state)
    plan = engine.analyze((str(vault),))
    result = engine.execute(plan, (str(vault),))
    assert result.status == "success"
    return result


def test_scan_is_read_only_exact_and_defaults_to_no_selection(application, tmp_path):
    root = tmp_path / "仓库集合"
    vault = make_vault(root, "课程")
    note = vault / ".trash" / "旧笔记.md"
    note.write_text("待恢复", encoding="utf-8")
    nested = vault / "项目" / ".trash" / "必须保留.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("keep", encoding="utf-8")
    ordinary = root / "普通目录" / ".trash"
    ordinary.mkdir(parents=True)
    (ordinary / "也保留.md").write_text("keep", encoding="utf-8")
    state = tmp_path / "state"
    before = {
        path.relative_to(root).as_posix(): (
            path.is_dir(), b"" if path.is_dir() else path.read_bytes()
        )
        for path in root.rglob("*")
    }
    page = TrashCleanupPage(state, {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        run_last(page, operations, page._start_scan)

        assert not state.exists()
        assert page.vault_model.rowCount() == 1
        assert page.vault_model.checked_previews() == ()
        assert page.entry_model.rowCount() == 1
        assert page.entry_model.rows[0].absolute_path == str(note)
        assert "1 个文件" in page.entry_summary.text()
        assert not page.clear_button.isEnabled()
        after = {
            path.relative_to(root).as_posix(): (
                path.is_dir(), b"" if path.is_dir() else path.read_bytes()
            )
            for path in root.rglob("*")
        }
        assert after == before

        page.entry_table.selectRow(0)
        page.copy_selected_entry_paths()
        assert QApplication.clipboard().text() == str(note)

        # TrashProgress intentionally has fewer optional fields than mirror
        # progress; the shared page protocol must still render it safely.
        page.task_progress(TrashProgress("hash", str(vault), "旧笔记.md", 2, 4))
        assert page.progress_bar.value() == 500
    finally:
        dispose(page)


def test_selection_requires_fresh_confirmation_and_path_change_invalidates(application, tmp_path):
    root = tmp_path / "root"
    vault = make_vault(root, "vault")
    (vault / ".trash" / "remove.md").write_text("remove", encoding="utf-8")
    page = TrashCleanupPage(tmp_path / "state", {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        run_last(page, operations, page._start_scan)
        check_vault(page)
        assert "1 个文件" in page.clear_confirm.text()
        assert not page.clear_button.isEnabled()

        page.clear_confirm.setChecked(True)
        assert page.clear_button.isEnabled()
        check_vault(page, checked=False)
        assert page.plan is not None
        assert not page.clear_confirm.isChecked()
        assert not page.clear_button.isEnabled()

        page.root_picker.set_value(str(root / "other"))
        assert page.plan is None
        assert page.vault_model.rowCount() == 0
        assert page.entry_model.rowCount() == 0
    finally:
        dispose(page)


def test_cleanup_and_new_page_restore_persistent_quarantine(application, tmp_path):
    root = tmp_path / "root"
    vault = make_vault(root, "vault")
    note = vault / ".trash" / "restore.md"
    note.write_text("restore me", encoding="utf-8")
    state = tmp_path / "state"
    settings = {"root": str(root)}
    page = TrashCleanupPage(state, settings, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    recovered = None
    try:
        run_last(page, operations, page._start_scan)
        check_vault(page)
        page.clear_confirm.setChecked(True)
        result = run_last(page, operations, page._start_clear)

        assert result.status == "success"
        assert list((vault / ".trash").iterdir()) == []
        assert page.current_operation is not None
        assert page.restore_button.isEnabled()

        recovered = TrashCleanupPage(state, settings, str(root))
        recovered_operations: list = []
        recovered.task_requested.connect(recovered_operations.append)
        assert recovered.current_operation is not None
        assert recovered.current_operation.operation_id == result.operation_id
        restored = run_last(recovered, recovered_operations, recovered._start_restore)

        assert restored.status == "success"
        assert note.read_text(encoding="utf-8") == "restore me"
        assert recovered.current_operation is not None
        assert {record.status for record in recovered.current_operation.records} == {"restored"}
        assert not recovered.restore_button.isEnabled()
        assert not recovered.finalize_button.isEnabled()
        recovered.finalize_confirm.setChecked(True)
        assert recovered.finalize_button.isEnabled()
    finally:
        if recovered is not None:
            dispose(recovered)
        dispose(page)


def test_finalize_requires_confirmation_and_reports_actual_released_bytes(application, tmp_path):
    root = tmp_path / "root"
    vault = make_vault(root, "vault")
    payload = b"final bytes"
    (vault / ".trash" / "final.bin").write_bytes(payload)
    state = tmp_path / "state"
    page = TrashCleanupPage(state, {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        run_last(page, operations, page._start_scan)
        check_vault(page)
        page.clear_confirm.setChecked(True)
        cleared = run_last(page, operations, page._start_clear)
        assert cleared.status == "success"
        operation_id = cleared.operation_id

        before = len(operations)
        page._start_finalize()
        assert len(operations) == before
        page.finalize_confirm.setChecked(True)
        finalized = run_last(page, operations, page._start_finalize)

        assert finalized.status == "success"
        assert finalized.bytes_freed == len(payload)
        assert page.current_operation is not None
        assert {record.status for record in page.current_operation.records} == {"finalized"}
        assert page.operation_selector.count() == 1
        assert not page.has_pending_recovery()
        assert not page.restore_button.isEnabled()
        assert not page.finalize_button.isEnabled()
        assert "实际释放" in page.status_label.text()
        assert not (state / "trash_backups" / str(operation_id)).exists()
    finally:
        dispose(page)


def test_all_pending_trash_operations_remain_selectable_and_restore_uses_selection(
    application, tmp_path
):
    root = tmp_path / "root"
    first_vault = make_vault(root, "first")
    second_vault = make_vault(root, "second")
    (first_vault / ".trash" / "first.md").write_text("first", encoding="utf-8")
    (second_vault / ".trash" / "second.md").write_text("second", encoding="utf-8")
    state = tmp_path / "state"
    first = create_pending_trash_operation(state, first_vault)
    second = create_pending_trash_operation(state, second_vault)
    page = TrashCleanupPage(state, {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        assert page.has_pending_recovery()
        assert page.operation_selector.count() == 2
        assert {
            page.operation_selector.itemData(index)
            for index in range(page.operation_selector.count())
        } == {first.operation_id, second.operation_id}

        page.finalize_confirm.setChecked(True)
        first_index = page.operation_selector.findData(first.operation_id)
        assert first_index >= 0
        page.operation_selector.setCurrentIndex(first_index)
        assert page.current_operation is not None
        assert page.current_operation.operation_id == first.operation_id
        assert not page.finalize_confirm.isChecked()

        restored = run_last(page, operations, page._start_restore)

        assert restored.status == "success"
        assert (first_vault / ".trash" / "first.md").read_text(
            encoding="utf-8"
        ) == "first"
        assert list((second_vault / ".trash").iterdir()) == []
        assert page.operation_selector.count() == 2
        assert page.current_operation is not None
        assert page.current_operation.operation_id == first.operation_id
        assert not page.restore_button.isEnabled()

        second_index = page.operation_selector.findData(second.operation_id)
        page.operation_selector.setCurrentIndex(second_index)
        assert page.current_operation is not None
        assert page.current_operation.operation_id == second.operation_id
        assert page.restore_button.isEnabled()

        page.resize(816, 620)
        page.show()
        application.processEvents()
        assert page.minimumSizeHint().width() <= 780
    finally:
        dispose(page)
