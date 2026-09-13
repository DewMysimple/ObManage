from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

import obmanage.management.trash as trash_module
from obmanage.management.trash import TrashProgress
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


def set_vault_checked(page: TrashCleanupPage, row: int, checked: bool) -> None:
    index = page.vault_model.index(row, 0)
    assert page.vault_model.setData(
        index,
        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
        Qt.ItemDataRole.CheckStateRole,
    )


def test_scan_is_read_only_exact_and_defaults_to_all_nonempty(application, tmp_path):
    root = tmp_path / "仓库集合"
    vault = make_vault(root, "课程")
    empty = make_vault(root, "空仓库")
    note = vault / ".trash" / "旧笔记.md"
    note.write_text("待清理", encoding="utf-8")
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
        assert page.vault_model.rowCount() == 2
        assert {item.vault_root for item in page.vault_model.checked_previews()} == {str(vault)}
        empty_row = next(
            index for index, item in enumerate(page.vault_model.rows)
            if item.vault_root == str(empty)
        )
        assert not page.vault_model.flags(page.vault_model.index(empty_row, 0)) & Qt.ItemFlag.ItemIsUserCheckable
        assert page.entry_model.rowCount() == 1
        assert page.entry_model.rows[0].absolute_path == str(note)
        assert "1 个文件" in page.entry_summary.text()
        assert "已默认选择 1 个" in page.status_label.text()
        assert page.clear_confirm.isEnabled()
        assert not page.clear_confirm.isChecked()
        assert not page.clear_button.isEnabled()
        assert not hasattr(page, "copy_vault_button")
        assert not hasattr(page, "copy_entry_button")
        assert page.legacy_recovery_panel.isHidden()
        after = {
            path.relative_to(root).as_posix(): (
                path.is_dir(), b"" if path.is_dir() else path.read_bytes()
            )
            for path in root.rglob("*")
        }
        assert after == before

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
        assert len(page.vault_model.checked_previews()) == 1
        assert "1 个文件" in page.clear_confirm.text()
        assert "无法恢复" in page.clear_confirm.text()
        assert not page.clear_button.isEnabled()

        page.clear_confirm.setChecked(True)
        assert page.clear_button.isEnabled()
        set_vault_checked(page, 0, False)
        assert page.plan is not None
        assert not page.clear_confirm.isChecked()
        assert not page.clear_button.isEnabled()

        set_vault_checked(page, 0, True)
        page.clear_confirm.setChecked(True)
        page.invalidate_confirmation()
        assert not page.clear_confirm.isChecked()

        page.root_picker.set_value(str(root / "other"))
        assert page.plan is None
        assert page.vault_model.rowCount() == 0
        assert page.entry_model.rowCount() == 0
    finally:
        dispose(page)


def test_direct_cleanup_keeps_unselected_vault_and_creates_no_recovery(application, tmp_path):
    root = tmp_path / "root"
    first = make_vault(root, "first")
    second = make_vault(root, "second")
    first_note = first / ".trash" / "remove.md"
    second_note = second / ".trash" / "keep.md"
    first_note.write_text("remove", encoding="utf-8")
    second_note.write_text("keep", encoding="utf-8")
    state = tmp_path / "state"
    page = TrashCleanupPage(state, {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    recovered = None
    try:
        run_last(page, operations, page._start_scan)
        second_row = next(
            index for index, item in enumerate(page.vault_model.rows)
            if item.vault_root == str(second)
        )
        set_vault_checked(page, second_row, False)
        page.clear_confirm.setChecked(True)
        result = run_last(page, operations, page._start_clear)

        assert result.status == "success"
        assert result.operation_id is None
        assert list((first / ".trash").iterdir()) == []
        assert second_note.read_text(encoding="utf-8") == "keep"
        assert (first / ".trash").is_dir()
        assert page.plan is None
        assert not page.has_pending_recovery()
        assert page.legacy_recovery_panel.isHidden()
        assert "直接清理" in page.status_label.text()
        assert "无法恢复" in page.status_label.text()
        assert not (state / "trash_backups").exists()
        assert not (state / "trash_journal").exists()
        assert not (state / "trash-auth.key").exists()

        recovered = TrashCleanupPage(state, {"root": str(root)}, str(root))
        assert not recovered.has_pending_recovery()
        assert recovered.legacy_recovery_panel.isHidden()
    finally:
        if recovered is not None:
            dispose(recovered)
        dispose(page)


def test_partial_direct_cleanup_reports_irreversible_counts(application, tmp_path, monkeypatch):
    root = tmp_path / "root"
    vault = make_vault(root, "vault")
    good = vault / ".trash" / "good.md"
    locked = vault / ".trash" / "locked.md"
    good.write_text("good", encoding="utf-8")
    locked.write_text("locked", encoding="utf-8")
    page = TrashCleanupPage(tmp_path / "state", {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    real_unlink = trash_module._unlink_file

    def fail_locked(path: str, expected):
        if Path(path).name == "locked.md":
            raise PermissionError("locked for page test")
        return real_unlink(path, expected)

    monkeypatch.setattr(trash_module, "_unlink_file", fail_locked)
    try:
        run_last(page, operations, page._start_scan)
        page.clear_confirm.setChecked(True)
        result = run_last(page, operations, page._start_clear)

        assert result.status == "partial"
        assert not good.exists()
        assert locked.exists()
        assert page.plan is None
        assert "已直接删除 1 个文件" in page.status_label.text()
        assert "无法恢复" in page.status_label.text()
    finally:
        dispose(page)


def test_cancelled_direct_cleanup_reports_irreversible_counts(application, tmp_path):
    root = tmp_path / "root"
    vault = make_vault(root, "vault")
    (vault / ".trash" / "a.md").write_bytes(b"aaaa")
    (vault / ".trash" / "b.md").write_bytes(b"bbbbbb")
    page = TrashCleanupPage(tmp_path / "state", {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        run_last(page, operations, page._start_scan)
        page.clear_confirm.setChecked(True)
        before = len(operations)
        page._start_clear()
        assert len(operations) == before + 1
        cancelled = threading.Event()

        def stop_after_first(event):
            if event.phase == "clear" and event.completed_bytes:
                cancelled.set()

        result = operations[-1](cancelled, stop_after_first)
        page.task_finished("ok", result)

        assert result.status == "cancelled"
        assert "已直接删除 1 个文件、0 个目录" in page.status_label.text()
        assert "已移除内容大小 4 B" in page.status_label.text()
        assert "无法恢复" in page.status_label.text()
    finally:
        dispose(page)


def test_legacy_quarantine_panel_only_appears_for_upgrade_data(
    application, tmp_path, seed_legacy_quarantine
):
    root = tmp_path / "root"
    vault = make_vault(root, "vault")
    note = vault / ".trash" / "restore.md"
    note.write_text("restore me", encoding="utf-8")
    state = tmp_path / "state"
    operation_id, _ = seed_legacy_quarantine(state, vault)
    page = TrashCleanupPage(state, {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        assert page.has_pending_recovery()
        assert not page.legacy_recovery_panel.isHidden()
        assert page.operation_selector.findData(operation_id) >= 0

        restored = run_last(page, operations, page._start_restore)
        assert restored.status == "success"
        assert note.read_text(encoding="utf-8") == "restore me"
        assert page.has_pending_recovery()

        page.finalize_confirm.setChecked(True)
        finalized = run_last(page, operations, page._start_finalize)
        assert finalized.status == "success"
        assert not page.has_pending_recovery()
        assert page.legacy_recovery_panel.isHidden()
    finally:
        dispose(page)


def test_all_legacy_quarantine_batches_remain_selectable(
    application, tmp_path, seed_legacy_quarantine
):
    root = tmp_path / "root"
    first = make_vault(root, "first")
    second = make_vault(root, "second")
    first_note = first / ".trash" / "first.md"
    second_note = second / ".trash" / "second.md"
    first_note.write_text("first", encoding="utf-8")
    second_note.write_text("second", encoding="utf-8")
    state = tmp_path / "state"
    first_id, _ = seed_legacy_quarantine(state, first)
    second_id, _ = seed_legacy_quarantine(state, second)
    page = TrashCleanupPage(state, {"root": str(root)}, str(root))
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        assert page.operation_selector.count() == 2
        assert {
            page.operation_selector.itemData(index)
            for index in range(page.operation_selector.count())
        } == {first_id, second_id}

        first_index = page.operation_selector.findData(first_id)
        page.operation_selector.setCurrentIndex(first_index)
        restored = run_last(page, operations, page._start_restore)

        assert restored.status == "success"
        assert first_note.read_text(encoding="utf-8") == "first"
        assert list((second / ".trash").iterdir()) == []
        assert page.operation_selector.count() == 2
        second_index = page.operation_selector.findData(second_id)
        assert second_index >= 0
        page.operation_selector.setCurrentIndex(second_index)
        assert page.restore_button.isEnabled()
    finally:
        dispose(page)
