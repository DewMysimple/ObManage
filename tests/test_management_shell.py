from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel

from obmanage.management.deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentRequest,
    DeploymentSelection,
    DeploymentTarget,
)
from obmanage.management.trash import TrashCleanupEngine
from obmanage.models import PlanItem, SyncCancelled, SyncPlan
from obmanage.pages.distribution import ObsidianConfigPage, TemplateSuitePage, TemplaterPage
from obmanage.pages.registry import FEATURES
from obmanage.pages.statistics import StatisticsPage
from obmanage.pages.trash_cleanup import TrashCleanupPage
from obmanage.settings import SettingsStore
from obmanage.ui import MainWindow


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


def wait_until(app, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    assert condition()


def dispose(app, window):
    if window.busy:
        window.cancel_operation()
        wait_until(app, lambda: not window.busy)
    window._timer.stop()
    window._save_timer.stop()
    window.tray.hide()
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_shell_registers_six_pages_with_mirror_first(application, tmp_path):
    window = MainWindow(tmp_path / "state")
    try:
        assert tuple(window.pages) == tuple(feature.key for feature in FEATURES)
        assert tuple(window.pages)[0] == "mirror"
        assert window.page_stack.currentWidget() is window.mirror_page
        assert window.navigation_buttons["mirror"].isChecked()
        assert "仓库镜像" in window.windowTitle()
        assert isinstance(window.pages["statistics"], StatisticsPage)
        assert isinstance(window.pages["template_suite"], TemplateSuitePage)
        assert isinstance(window.pages["obsidian_config"], ObsidianConfigPage)
        assert isinstance(window.pages["templater"], TemplaterPage)
        assert isinstance(window.pages["trash_cleanup"], TrashCleanupPage)
        assert "页面正在初始化…" not in {label.text() for label in window.findChildren(QLabel)}
    finally:
        dispose(application, window)


def test_leaving_mirror_revokes_only_confirmation_and_persists_page(application, tmp_path):
    state_dir = tmp_path / "state"
    window = MainWindow(state_dir)
    try:
        source, target = window._paths()
        window._accept_plan(
            SyncPlan(source, target, [PlanItem("add", "note.md", 4, "new")]),
            scheduled=False,
        )
        window.confirm_direction_checkbox.setChecked(True)
        assert window.plan is not None

        window._show_page("statistics")

        assert window.plan is not None
        assert not window.confirm_direction_checkbox.isChecked()
        assert window.page_stack.currentWidget() is window.page_containers["statistics"]
        window._persist()
        assert SettingsStore(state_dir).load_document().selected_page == "statistics"
    finally:
        dispose(application, window)


def test_feature_worker_owns_global_slot_and_cancels_without_residual_thread(application, tmp_path):
    window = MainWindow(tmp_path / "state")
    page = window.pages["statistics"]
    try:
        def operation(cancel, progress):
            if cancel.wait(5):
                raise SyncCancelled("cancelled")
            raise RuntimeError("cancel was not delivered")

        page.start_task("probe", operation)
        assert window.busy
        assert not window.analyze_button.isEnabled()
        window._show_page("templater")
        assert not window.nav_cancel_button.isHidden()
        window.scheduler.next_run = datetime.now() - timedelta(seconds=1)
        window._timer_tick()
        assert window.busy
        assert window.scheduler.next_run > datetime.now()

        window.cancel_operation()
        wait_until(application, lambda: not window.busy)

        assert window._feature_worker is None
        assert not page._task_active
        assert window.analyze_button.isEnabled()
        assert window.nav_cancel_button.isHidden()
    finally:
        dispose(application, window)


def test_statistics_page_runs_read_only_service_and_reports_totals(application, tmp_path):
    root = tmp_path / "仓库集合"
    vault = root / "课程笔记"
    (vault / ".obsidian").mkdir(parents=True)
    note = vault / "第一课.md"
    note.write_text("你好\nObManage", encoding="utf-8")
    before = {
        str(path.relative_to(root)): (path.is_dir(), path.read_bytes() if path.is_file() else b"")
        for path in root.rglob("*")
    }
    window = MainWindow(tmp_path / "state")
    page = window.pages["statistics"]
    try:
        window._show_page("statistics")
        page.root_picker.set_value(str(root))
        page.count_characters.setChecked(True)
        page.include_trash.setChecked(False)
        page._start_scan()
        wait_until(application, lambda: not window.busy)

        assert page.model.rowCount() == 1
        assert page.summary_values["vaults"].text() == "1"
        assert page.summary_values["notes"].text() == "1"
        assert page.summary_values["characters"].text() == str(
            len(note.read_bytes().decode("utf-8"))
        )
        assert "统计完成" in page.status_label.text()
        after = {
            str(path.relative_to(root)): (path.is_dir(), path.read_bytes() if path.is_file() else b"")
            for path in root.rglob("*")
        }
        assert after == before
    finally:
        dispose(application, window)


def test_pending_quarantine_blocks_all_new_writes_and_due_mirror(application, tmp_path):
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash" / "recover.md").write_text("recover", encoding="utf-8")
    engine = TrashCleanupEngine(state)
    trash_plan = engine.analyze((vault,))
    cleared = engine.execute(trash_plan, (vault,))
    assert cleared.status == "success"

    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    window = MainWindow(state)
    try:
        window.local_edit.setText(str(source))
        window.portable_combo.setCurrentText(str(target))
        window.set_direction("to_portable")
        window._accept_plan(
            SyncPlan(str(source), str(target), [PlanItem("add", "note.md", 4, "new")]),
            scheduled=False,
        )
        window.confirm_direction_checkbox.setChecked(True)
        window._refresh_actions()

        assert not window.nav_recovery_button.isHidden()
        assert not window.sync_button.isEnabled()
        assert window.pages["trash_cleanup"].has_pending_recovery()
        for key in ("template_suite", "obsidian_config", "templater"):
            assert window.pages[key]._external_recovery_pending

        window._execute_plan()
        assert not window.busy
        assert "待恢复事务" in window.status_label.text()

        window.scheduler.next_run = datetime.now() - timedelta(seconds=1)
        window._timer_tick()
        assert not window.busy
        assert window.scheduler.next_run > datetime.now()
        assert any("待恢复事务" in line and "跳过" in line for line in window._log_lines)

        window.nav_recovery_button.click()
        assert window._current_page_key == "trash_cleanup"

        finalized = engine.finalize(cleared.operation_id)
        assert finalized.status == "success"
        window.pages["trash_cleanup"]._reload_operations()
        window._refresh_actions()
        assert window.nav_recovery_button.isHidden()
    finally:
        dispose(application, window)


def test_unreadable_deployment_journal_fails_closed_across_pages(application, tmp_path):
    state = tmp_path / "state"
    journal = state / "deployment-journal"
    journal.mkdir(parents=True)
    (journal / "not-a-batch.json").write_text("{}", encoding="utf-8")

    window = MainWindow(state)
    try:
        blocked_pages = tuple(
            window.pages[key] for key in ("template_suite", "obsidian_config", "templater")
        )
        assert all(page._recovery_blocked for page in blocked_pages)
        assert all(page.has_pending_recovery() for page in blocked_pages)
        assert not window.nav_recovery_button.isHidden()
        assert not window.sync_button.isEnabled()
        for page in blocked_pages:
            page._start_execute()
            assert "已阻止" in page.status_label.text()
    finally:
        dispose(application, window)


def test_unknown_legacy_deployment_is_globally_blocked_and_reachable(
        application, tmp_path):
    source = tmp_path / "legacy-source"
    target = tmp_path / "target"
    (source / ".obsidian").mkdir(parents=True)
    (target / ".obsidian").mkdir(parents=True)
    (source / ".obsidian" / "value.json").write_text("new", encoding="utf-8")
    (target / ".obsidian" / "value.json").write_text("old", encoding="utf-8")
    state = tmp_path / "state"
    engine = DeploymentEngine(state)
    request = DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source),
        DeploymentTarget("legacy-target", str(target)),
    ),), label="legacy-distribution-v0")
    plan = engine.analyze(request)
    assert engine.execute(plan).success

    window = MainWindow(state)
    try:
        assert "obsidian_config" in window._recovery_page_keys()
        assert not window.nav_recovery_button.isHidden()
        assert window.pages["obsidian_config"].has_pending_recovery()
        assert window.pages["template_suite"]._external_recovery_pending
        assert window.pages["trash_cleanup"]._external_recovery_pending

        window.nav_recovery_button.click()

        assert window._current_page_key == "obsidian_config"
        page = window.pages["obsidian_config"]
        assert page.current_batch is not None
        assert page.current_batch.batch_id == plan.batch_id
        assert "旧版/未知入口" in page.batch_selector.currentText()
    finally:
        dispose(application, window)


def test_global_cancel_is_hidden_for_non_cancellable_recovery_task(application, tmp_path):
    window = MainWindow(tmp_path / "state")
    page = window.pages["obsidian_config"]
    release = Event()
    try:
        def operation(_cancel, _progress):
            release.wait(2)
            raise RuntimeError("probe finished")

        page.start_task("rollback", operation)
        wait_until(application, lambda: window.busy)

        assert window.nav_cancel_button.isHidden()
        assert page.cancel_button.isHidden()

        release.set()
        wait_until(application, lambda: not window.busy)
    finally:
        release.set()
        dispose(application, window)


def test_state_inside_configured_repository_blocks_ui_state_and_tasks(
        application, tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    state = source / "app-state"
    window = MainWindow(state)
    try:
        window.local_edit.setText(str(source))
        window.portable_combo.setCurrentText(str(target))
        window.set_direction("to_portable")
        window._refresh_actions()

        assert not window._persist()
        window._log("这条消息只能保留在内存")
        window.analyze()

        assert not window.busy
        assert not state.exists()
        assert not window.analyze_button.isEnabled()
        assert "程序数据目录" in window.status_label.text()

        page = window.pages["statistics"]
        page.start_task("scan", lambda _cancel, _progress: None)
        assert not window.busy
        assert "程序数据目录" in page.status_label.text()
        assert not state.exists()
    finally:
        dispose(application, window)


def test_management_page_root_cannot_enclose_state_directory(application, tmp_path):
    collection = tmp_path / "collection"
    state = collection / "app-state"
    collection.mkdir()
    window = MainWindow(state)
    page = window.pages["statistics"]
    try:
        page.root_picker.set_value(str(collection))

        assert not window._persist()
        page.start_task("scan", lambda _cancel, _progress: None)

        assert not window.busy
        assert not state.exists()
        assert "程序数据目录" in page.status_label.text()
    finally:
        dispose(application, window)
