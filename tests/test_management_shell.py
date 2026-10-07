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
from obmanage.pages.backup import VaultBackupPage
from obmanage.pages.incremental import IncrementalPage
from obmanage.pages.distribution import ComSyncPage
from obmanage.pages.registry import FEATURES
from obmanage.pages.statistics import StatisticsPage
from obmanage.pages.trash_cleanup import TrashCleanupPage
from obmanage.settings import SettingsStore
from obmanage.ui import MainWindow
from obmanage.pages.common import PathPicker
from obmanage.pages.vault_selection import VaultSelection
from obmanage.management.models import VaultCatalogResult, VaultInfo


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


def test_shell_registers_eight_pages_with_mirror_first(application, tmp_path):
    window = MainWindow(tmp_path / "state")
    try:
        assert tuple(window.pages) == tuple(feature.key for feature in FEATURES)
        assert tuple(window.pages)[0] == "mirror"
        assert len(window.pages) == 8
        assert window.page_stack.currentWidget() is window.mirror_page
        assert window.navigation_buttons["mirror"].isChecked()
        assert "仓库镜像" in window.windowTitle()
        assert isinstance(window.pages["vault_backup"], VaultBackupPage)
        assert isinstance(window.pages["incremental"], IncrementalPage)
        assert isinstance(window.pages["statistics"], StatisticsPage)
        assert isinstance(window.pages["comsync"], ComSyncPage)
        assert isinstance(window.pages["trash_cleanup"], TrashCleanupPage)
        assert "页面正在初始化…" not in {label.text() for label in window.findChildren(QLabel)}
    finally:
        dispose(application, window)


def test_all_path_selectors_share_three_choices_and_runtime_uses_single_task_slot(application, tmp_path, monkeypatch):
    from obmanage.pages import vault_selection
    vault = tmp_path / "Live"
    (vault / ".obsidian").mkdir(parents=True)
    entered, release = Event(), Event()
    def runtime(_config, *, cancel):
        entered.set()
        while not release.wait(.01):
            if cancel.is_set():
                raise SyncCancelled()
        return VaultCatalogResult((VaultInfo(str(vault), "Live"),))
    monkeypatch.setattr(vault_selection, "read_running_vaults", runtime)
    window = MainWindow(tmp_path / "state")
    try:
        for page in window.pages.values():
            for picker in page.findChildren(PathPicker):
                assert picker.vault_list.text() == "仓库列表"
                assert picker.running_vault.text() == "当前运行"
                assert picker.browse.text() == "浏览…"
        page = window.pages["comsync"]
        window._show_page("comsync", persist=False)
        page.source_picker.running_vault.click()
        wait_until(application, entered.is_set)
        assert window.busy
        assert isinstance(window._feature_page, VaultSelection)
        assert window._feature_page.task_cancellable()
        assert not window.analyze_button.isEnabled()
        assert not window.local_selection.buttons[0].isEnabled()
        assert not page.source_picker.browse.isEnabled()
        release.set()
        wait_until(application, lambda: not window.busy)
        assert page.source_picker.value == str(vault)
        assert not page.confirm_checkbox.isChecked()
        assert page.source_picker.vault_list.isEnabled()
        # Mirror uses the same adapter and ordinary input invalidation.
        window.plan = SyncPlan("old", "target", [PlanItem("add", "x")])
        window.local_selection.start(True)
        wait_until(application, lambda: not window.busy)
        assert window.local_edit.text() == str(vault)
        assert window.plan is None
    finally:
        release.set()
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
        window._show_page("comsync")
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
    video = vault / "演示.mp4"
    video.write_bytes(b"video-data")
    reference_vault = root / "参考资料"
    (reference_vault / ".obsidian").mkdir(parents=True)
    reference = reference_vault / "规范.pdf"
    reference.write_bytes(b"pdf-data")
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

        assert page.model.rowCount() == 2
        assert page.summary_values["vaults"].text() == "2"
        assert page.summary_values["files"].text() == "3"
        assert page.summary_values["size"].text() == (
            f"{note.stat().st_size + video.stat().st_size + reference.stat().st_size} B"
        )
        assert page.summary_values["notes"].text() == "1"
        assert page.summary_values["characters"].text() == str(
            len(note.read_bytes().decode("utf-8"))
        )
        type_rows = {item.label: item for item in page.type_model.rows}
        assert set(type_rows) == {"Markdown", "视频", "PDF"}
        assert type_rows["视频"].files == 1
        assert type_rows["视频"].total_bytes == video.stat().st_size
        source_row = next(
            index for index, item in enumerate(page.model.rows)
            if item.vault_path == str(vault)
        )
        proxy_index = page.proxy.mapFromSource(page.model.index(source_row, 0))
        page.table.selectRow(proxy_index.row())
        application.processEvents()
        assert "课程笔记" in page.type_scope_label.text()
        assert {item.label for item in page.type_model.rows} == {"Markdown", "视频"}
        page.show_all_types_button.click()
        application.processEvents()
        assert page.type_scope_label.text() == "全部仓库"
        assert set(item.label for item in page.type_model.rows) == {"Markdown", "视频", "PDF"}
        assert "统计完成" in page.status_label.text()
        after = {
            str(path.relative_to(root)): (path.is_dir(), path.read_bytes() if path.is_file() else b"")
            for path in root.rglob("*")
        }
        assert after == before
    finally:
        dispose(application, window)


def test_direct_trash_cleanup_creates_no_recovery_gate(application, tmp_path):
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash" / "remove.md").write_text("remove", encoding="utf-8")
    engine = TrashCleanupEngine(state)
    trash_plan = engine.analyze((vault,))
    cleared = engine.execute(trash_plan, (vault,))
    assert cleared.status == "success"
    assert cleared.operation_id is None
    assert not (state / "trash_backups").exists()
    assert not (state / "trash_journal").exists()

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

        assert window.nav_recovery_button.isHidden()
        assert window.sync_button.isEnabled()
        assert not window.pages["trash_cleanup"].has_pending_recovery()
        for key in ("vault_backup", "comsync"):
            assert not window.pages[key]._external_recovery_pending
    finally:
        dispose(application, window)


def test_legacy_trash_quarantine_still_blocks_writes_until_finalized(
    application, tmp_path, seed_legacy_quarantine
):
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash" / "recover.md").write_text("recover", encoding="utf-8")
    operation_id, _ = seed_legacy_quarantine(state, vault)
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

        assert window._recovery_page_keys() == ("trash_cleanup",)
        assert not window.nav_recovery_button.isHidden()
        assert not window.sync_button.isEnabled()
        assert window.pages["trash_cleanup"].has_pending_recovery()
        assert window.pages["vault_backup"]._external_recovery_pending
        for key in ("comsync",):
            assert window.pages[key]._external_recovery_pending
        window._execute_plan()
        assert not window.busy

        window.scheduler.next_run = datetime.now() - timedelta(seconds=1)
        window._timer_tick()
        assert not window.busy
        assert window.scheduler.next_run > datetime.now()
        assert any("待恢复事务" in line and "跳过" in line for line in window._log_lines)
        window.nav_recovery_button.click()
        assert window._current_page_key == "transactions"

        finalized = TrashCleanupEngine(state).finalize(operation_id)
        assert finalized.status == "success"
        window.pages["trash_cleanup"]._reload_operations()
        window._refresh_actions()
        assert window._recovery_page_keys() == ()
        assert window.nav_recovery_button.isHidden()
        window._show_page("mirror")
        window.confirm_direction_checkbox.setChecked(True)
        window._refresh_actions()
        assert window.sync_button.isEnabled()
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
            window.pages[key] for key in ("comsync",)
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
        assert "comsync" in window._recovery_page_keys()
        assert not window.nav_recovery_button.isHidden()
        assert window.pages["comsync"].has_pending_recovery()
        assert window.pages["archive"]._external_recovery_pending
        assert window.pages["vault_backup"]._external_recovery_pending
        assert window.pages["trash_cleanup"]._external_recovery_pending

        window.nav_recovery_button.click()

        assert window._current_page_key == "transactions"
        page = window.pages["comsync"]
        assert page.current_batch is not None
        assert page.current_batch.batch_id == plan.batch_id
        assert "旧版" not in page.batch_selector.currentText()
    finally:
        dispose(application, window)


def test_global_cancel_is_hidden_for_non_cancellable_recovery_task(application, tmp_path):
    window = MainWindow(tmp_path / "state")
    page = window.pages["comsync"]
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


def test_resolving_changed_deployment_releases_all_pages_through_worker(application, tmp_path):
    source, target, state = (tmp_path / name for name in ("source", "target", "state"))
    (source / ".obsidian").mkdir(parents=True)
    (target / ".obsidian").mkdir(parents=True)
    (source / ".obsidian" / "settings.json").write_text("new")
    (target / ".obsidian" / "settings.json").write_text("old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target)),
    ),), label="legacy-v1"))
    assert engine.execute(plan).success
    (target / ".obsidian" / "workspace.json").write_text("later")
    assert not engine.rollback(plan.batch_id).success
    window = MainWindow(state)
    try:
        assert window._recovery_page_keys() == ("comsync",)
        window.nav_recovery_button.click()
        page = window.pages["comsync"]
        window.show()
        wait_until(application, lambda: not window.busy and page.recovery_preview is not None)
        assert page.recovery_preview is not None
        assert page.resolve_confirm.isHidden()
        page.resolve_button.click()
        wait_until(application, lambda: not window.busy)
        assert not window._recovery_page_keys()
        assert window.nav_recovery_button.isHidden()
        for key in ("incremental", "vault_backup", "archive", "trash_cleanup"):
            assert not window.pages[key]._external_recovery_pending
        assert engine.get_batch(plan.batch_id).status == "resolved"
        assert (target / ".obsidian" / "workspace.json").read_text() == "later"
        assert tuple(target.glob(".obmanage-deploy-*.backup"))
    finally:
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



def test_comsync_new_vault_full_worker_flow_and_rollback(application, tmp_path, monkeypatch):
    from obmanage.management.models import OpenedVaultCandidates
    source, target = tmp_path / "source", tmp_path / "collection" / "new"
    (source / ".obsidian").mkdir(parents=True)
    (source / ".obsidian/app.json").write_text("new")
    (source / "File/Templater").mkdir(parents=True)
    (source / "File/Templater/template.md").write_text("template")
    (source / "File/Note").mkdir()
    (source / "File/Note/private.md").write_text("private")
    (target / ".obsidian").mkdir(parents=True)
    window = MainWindow(tmp_path / "state")
    page = window.pages["comsync"]
    try:
        page.opened_vault_reader = lambda _: OpenedVaultCandidates()
        page.source_picker.set_value(str(source))
        page.root_picker.set_value(str(target.parent))
        page.component_boxes["file"].setChecked(True)
        window._show_page("comsync", persist=False)
        page.catalog_button.click()
        wait_until(application, lambda: not window.busy)
        assert page.latest_button.isEnabled()
        page.latest_button.click()
        assert len(page.target_model.checked_vaults()) == 1
        page.analyze_button.click()
        wait_until(application, lambda: not window.busy)
        assert page.plan and "1 个仓库" in page.confirm_checkbox.text()
        page.confirm_checkbox.setChecked(True)
        page.execute_button.click()
        wait_until(application, lambda: not window.busy)
        assert page.has_pending_recovery()
        assert (target / ".obsidian/app.json").read_text() == "new"
        assert (target / "File/Templater/template.md").read_text() == "template"
        assert list((target / "File/Note").iterdir()) == []
        assert list((target / "File/Attachment").iterdir()) == []
        assert (source / "File/Note/private.md").read_text() == "private"
        window._show_page("transactions", persist=False)
        window.show()
        wait_until(application, lambda: not window.busy and page.recovery_preview is not None)
        confirmations = []
        monkeypatch.setattr(window.pages["transactions"], "_confirm",
                            lambda *args: confirmations.append(args) or True)
        page.rollback_button.click()
        wait_until(application, lambda: not window.busy)
        assert len(confirmations) == 1
        assert not page.has_pending_recovery()
        assert not (target / "File").exists()
        assert not (target / ".obsidian/app.json").exists()
        assert any(row.feature == "comsync" and row.level == "success" for row in window._log_entries)
    finally:
        dispose(application, window)


def test_transactions_clear_through_worker_and_restart(application, tmp_path, monkeypatch):
    source, target, state = (tmp_path / name for name in ("source", "target", "state"))
    (source / ".obsidian").mkdir(parents=True)
    (target / ".obsidian").mkdir(parents=True)
    (source / ".obsidian" / "config.json").write_text("new")
    (target / ".obsidian" / "config.json").write_text("old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))),)))
    assert engine.execute(plan).success
    assert engine.resolve(plan.batch_id, expected_revision=engine.get_batch(plan.batch_id).revision).success
    window = MainWindow(state)
    try:
        page = window.pages["transactions"]
        controller = window.pages["comsync"]
        assert page.isAncestorOf(controller.recovery_details)
        assert controller.recovery_frame.isHidden()
        window._show_page("transactions")
        window.show()
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)
        assert page.clear_button.isEnabled()
        assert controller.inspect_button.isHidden()
        assert controller.recovery_details.isHidden()
        window._show_page("archive")
        assert controller.recovery_preview is None
        assert not page.clear_button.isEnabled()
        window._show_page("transactions")
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)
        confirmations = []
        monkeypatch.setattr(page, "_confirm", lambda *args: confirmations.append(args) or True)
        page.clear_button.click()
        wait_until(application, lambda: not window.busy)
        assert len(confirmations) == 1
        assert str(target) in confirmations[0][2]
        assert controller.batch_selector.count() == 0
        assert "已清除" in page.status_label.text()
        assert not tuple(target.glob(".obmanage-deploy-*.backup"))
        assert (target / ".obsidian/config.json").read_text() == "new"
        assert not window._recovery_page_keys()
    finally:
        dispose(application, window)
    fresh = MainWindow(state)
    try:
        assert fresh.pages["comsync"].batch_selector.count() == 0
        assert not fresh.pages["transactions"].clear_button.isEnabled()
    finally:
        dispose(application, fresh)


def test_clear_finalized_legacy_record(application, tmp_path, seed_legacy_quarantine, monkeypatch):
    state, vault = tmp_path / "state", tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash/note.md").write_text("old")
    operation_id, _ = seed_legacy_quarantine(state, vault)
    engine = TrashCleanupEngine(state)
    import pytest
    with pytest.raises(Exception, match="先永久清理"):
        engine.clear_record(operation_id)
    assert engine.finalize(operation_id).status == "success"
    window = MainWindow(state)
    try:
        window._show_page("transactions")
        page = window.pages["transactions"]
        assert page.clear_button.isEnabled()
        assert window.pages["trash_cleanup"].finalize_button.isHidden()
        confirmations = []
        monkeypatch.setattr(page, "_confirm", lambda *args: confirmations.append(args) or True)
        page.clear_button.click()
        wait_until(application, lambda: not window.busy)
        assert len(confirmations) == 1
        assert not engine.list_operations()
        assert window.pages["trash_cleanup"].legacy_recovery_panel.isHidden()
        assert (vault / ".trash").exists()
    finally:
        dispose(application, window)


def test_transaction_cancel_and_navigation_during_confirmation_do_not_write(
    application, tmp_path, monkeypatch
):
    source, target, state = (tmp_path / name for name in ("source", "target", "state"))
    (source / ".obsidian").mkdir(parents=True)
    (target / ".obsidian").mkdir(parents=True)
    (source / ".obsidian/new.json").write_text("new")
    (target / ".obsidian/old.json").write_text("old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))),)))
    assert engine.execute(plan).success
    assert engine.resolve(plan.batch_id, expected_revision=engine.get_batch(plan.batch_id).revision).success
    window = MainWindow(state)
    try:
        window._show_page("transactions", persist=False)
        window.show()
        controller, page = window.pages["comsync"], window.pages["transactions"]
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)
        revision = engine.get_batch(plan.batch_id).revision
        button = page.clear_button
        calls = []
        monkeypatch.setattr(page, "_confirm", lambda *args: calls.append(args) or False)
        button.click()
        assert len(calls) == 1
        assert not window.busy
        assert engine.get_batch(plan.batch_id).revision == revision
        assert tuple(target.glob(".obmanage-deploy-*.backup"))
        assert (target / ".obsidian/new.json").read_text() == "new"

        def leave_page(*args):
            window._show_page("archive", persist=False)
            return True

        monkeypatch.setattr(page, "_confirm", leave_page)
        button.click()
        assert not window.busy
        assert controller.recovery_preview is None
        assert engine.get_batch(plan.batch_id).revision == revision
        assert tuple(target.glob(".obmanage-deploy-*.backup"))
    finally:
        dispose(application, window)


def test_transaction_stale_backup_keeps_record_and_disables_clear(
    application, tmp_path, monkeypatch
):
    source, target, state = (tmp_path / name for name in ("source", "target", "state"))
    (source / ".obsidian").mkdir(parents=True)
    (target / ".obsidian").mkdir(parents=True)
    (source / ".obsidian/new.json").write_text("new")
    (target / ".obsidian/old.json").write_text("old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))),)))
    assert engine.execute(plan).success
    assert engine.resolve(plan.batch_id, expected_revision=engine.get_batch(plan.batch_id).revision).success
    window = MainWindow(state)
    try:
        window._show_page("transactions", persist=False)
        window.show()
        controller, page = window.pages["comsync"], window.pages["transactions"]
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)
        backup = next(target.glob(".obmanage-deploy-*.backup"))
        (backup / "unknown.txt").write_text("keep unknown data")
        monkeypatch.setattr(page, "_confirm", lambda *args: True)
        page.clear_button.click()
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)
        assert engine.get_batch(plan.batch_id).status == "resolved"
        assert (backup / "unknown.txt").read_text() == "keep unknown data"
        assert (backup / "old.json").read_text() == "old"
        assert (target / ".obsidian/new.json").read_text() == "new"
        assert not page.clear_button.isEnabled()
        assert not hasattr(page, "forget_button")
        assert "清除未完成" in page.status_label.text()
        assert not window._recovery_page_keys()
    finally:
        dispose(application, window)


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_legacy_clear_removes_record_once_and_retains_failed_batch(
    application, tmp_path, seed_legacy_quarantine, monkeypatch, fail_cleanup
):
    import obmanage.management.trash as trash_module
    state, vault = tmp_path / "state", tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash/note.md").write_text("old")
    operation_id, backup = seed_legacy_quarantine(state, vault)
    window = MainWindow(state)
    try:
        window._show_page("transactions", persist=False)
        page, controller = window.pages["transactions"], window.pages["trash_cleanup"]
        confirmations = []
        monkeypatch.setattr(page, "_confirm", lambda *args: confirmations.append(args) or True)
        if fail_cleanup:
            monkeypatch.setattr(trash_module, "_remove_backup_tree", lambda *args, **kwargs:
                                (_ for _ in ()).throw(PermissionError("injected cleanup failure")))
        page.clear_button.click()
        wait_until(application, lambda: not window.busy)
        assert len(confirmations) == 1
        assert "1 个文件" in confirmations[0][1]
        assert str(backup) in confirmations[0][2]
        assert (vault / ".trash").is_dir()
        assert not tuple((vault / ".trash").iterdir())
        operations = TrashCleanupEngine(state).list_operations()
        if fail_cleanup:
            assert operations[0].operation_id == operation_id
            assert backup.exists()
            assert window._recovery_page_keys() == ("trash_cleanup",)
        else:
            assert operations == ()
            assert not backup.exists()
            assert not window._recovery_page_keys()
    finally:
        dispose(application, window)


def test_transaction_confirmation_has_one_action_and_cancel_default(application, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    window = MainWindow(tmp_path / "state")
    try:
        page = window.pages["transactions"]
        dialogs = []

        def inspect(dialog):
            dialogs.append(dialog)
            assert dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.Cancel)
            assert dialog.button(QMessageBox.StandardButton.Ok).text() == "清除副本和记录"
            assert "1 个文件" in dialog.text()
            assert dialog.detailedText() == "demo/backup"
            return QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(QMessageBox, "exec", inspect)
        assert not page._confirm("清除副本和记录", "1 个文件，删除后不可恢复", "demo/backup")
        assert len(dialogs) == 1
    finally:
        dispose(application, window)


def test_transaction_selection_automatically_inspects_selected_batch(application, tmp_path):
    source, target, state = (tmp_path / name for name in ("source", "target", "state"))
    (source / ".obsidian").mkdir(parents=True)
    (target / ".obsidian").mkdir(parents=True)
    (target / ".obsidian/config.json").write_text("original")
    engine = DeploymentEngine(state)
    ids = []
    for value in ("first", "second"):
        (source / ".obsidian/config.json").write_text(value)
        plan = engine.analyze(DeploymentRequest((DeploymentSelection(
            DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))),)))
        assert engine.execute(plan).success
        assert engine.resolve(plan.batch_id, expected_revision=engine.get_batch(plan.batch_id).revision).success
        ids.append(plan.batch_id)
    revisions = {batch.batch_id: batch.revision for batch in engine.list_batches()}
    window = MainWindow(state)
    try:
        window._show_page("transactions", persist=False)
        window.show()
        controller, page = window.pages["comsync"], window.pages["transactions"]
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)
        for batch_id in ids:
            index = page.selector.findData(f"deploy:{batch_id}")
            page.selector.setCurrentIndex(index)
            wait_until(application, lambda: not window.busy and controller.recovery_preview is not None
                       and controller.recovery_preview.batch_id == batch_id)
            assert page.clear_button.isEnabled()
            assert not page.details_button.isChecked()
        assert {batch.batch_id: batch.revision for batch in engine.list_batches()} == revisions
        assert (target / ".obsidian/config.json").read_text() == "second"
        assert len(tuple(target.glob(".obmanage-deploy-*.backup"))) == 2
    finally:
        dispose(application, window)


@pytest.mark.parametrize("finalized", [False, True])
def test_legacy_confirmation_cancel_and_navigation_keep_selected_record(
    application, tmp_path, seed_legacy_quarantine, monkeypatch, finalized
):
    state, vault = tmp_path / "state", tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".trash/note.md").write_text("old")
    operation_id, backup = seed_legacy_quarantine(state, vault)
    engine = TrashCleanupEngine(state)
    if finalized:
        assert engine.finalize(operation_id).status == "success"
    window = MainWindow(state)
    try:
        window._show_page("transactions", persist=False)
        controller, page = window.pages["trash_cleanup"], window.pages["transactions"]
        button = page.clear_button
        monkeypatch.setattr(page, "_confirm", lambda *args: False)
        button.click()
        assert not window.busy
        assert engine.list_operations()[0].operation_id == operation_id

        def leave_page(*args):
            window._show_page("archive", persist=False)
            return True

        monkeypatch.setattr(page, "_confirm", leave_page)
        button.click()
        assert not window.busy
        assert engine.list_operations()[0].operation_id == operation_id
        if not finalized:
            assert (backup / "note.md").read_text() == "old"
    finally:
        dispose(application, window)


def test_mixed_transactions_share_one_selector_and_clear_action(
    application, tmp_path, seed_legacy_quarantine, monkeypatch
):
    from PySide6.QtWidgets import QComboBox, QPushButton
    state, source, target = (tmp_path / name for name in ("state", "source", "target"))
    for vault in (source, target):
        (vault / ".obsidian").mkdir(parents=True)
    (source / ".obsidian/config.json").write_text("new")
    (target / ".obsidian/config.json").write_text("old")
    deployment = DeploymentEngine(state)
    plan = deployment.analyze(DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))),), label="historical"))
    assert deployment.execute(plan).success
    assert deployment.resolve(plan.batch_id, expected_revision=deployment.get_batch(plan.batch_id).revision).success
    trash = TrashCleanupEngine(state)
    operations = []
    for name in ("pending", "finished"):
        vault = tmp_path / name
        (vault / ".obsidian").mkdir(parents=True)
        (vault / ".trash").mkdir()
        (vault / ".trash/note.md").write_text(name)
        identifier, backup = seed_legacy_quarantine(state, vault)
        operations.append((identifier, backup, vault))
    assert trash.finalize(operations[1][0]).status == "success"
    window = MainWindow(state)
    try:
        window._show_page("transactions", persist=False)
        window.show()
        page, controller = window.pages["transactions"], window.pages["comsync"]
        assert page.selector.count() == 3
        assert page.selector.currentData() == f"trash:{operations[0][0]}"
        assert [widget for widget in page.findChildren(QComboBox) if widget.isVisible()] == [page.selector]
        assert all("旧版" not in page.selector.itemText(index) for index in range(page.selector.count()))
        assert not any(button.text() in {"仅删除记录", "删除旧版记录"}
                       for button in page.findChildren(QPushButton))
        page.selector.setCurrentIndex(page.selector.findData(f"deploy:{plan.batch_id}"))
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)

        def switch_transaction(*_):
            page.selector.setCurrentIndex(page.selector.findData(f"trash:{operations[1][0]}"))
            return True

        monkeypatch.setattr(page, "_confirm", switch_transaction)
        page.clear_button.click()
        assert not window.busy
        assert len(deployment.list_batches()) == 1
        assert len(trash.list_operations()) == 2
        assert tuple(target.glob(".obmanage-deploy-*.backup"))
        page.selector.setCurrentIndex(page.selector.findData(f"deploy:{plan.batch_id}"))
        wait_until(application, lambda: not window.busy and controller.recovery_preview is not None)
        monkeypatch.setattr(page, "_confirm", lambda *_: True)
        page.clear_button.click()
        wait_until(application, lambda: not window.busy and page.selector.count() == 2)
        assert not deployment.list_batches()
        assert not tuple(target.glob(".obmanage-deploy-*.backup"))
        assert page.selector.currentData() == f"trash:{operations[0][0]}"
        page.details_button.click()
        assert str(operations[0][1]) in page.details.toPlainText()
        window.pages["trash_cleanup"].restore_button.click()
        wait_until(application, lambda: not window.busy)
        assert (operations[0][2] / ".trash/note.md").read_text() == "pending"
        for identifier, backup, _ in operations:
            page.selector.setCurrentIndex(page.selector.findData(f"trash:{identifier}"))
            assert page.clear_button.isEnabled()
            page.clear_button.click()
            wait_until(application, lambda: not window.busy)
            assert not backup.exists()
        assert not trash.list_operations()
        assert page.selector.count() == 0
        assert page.empty_label.isVisible()
        assert (target / ".obsidian/config.json").read_text() == "new"
        assert (operations[0][2] / ".trash/note.md").read_text() == "pending"
    finally:
        dispose(application, window)
