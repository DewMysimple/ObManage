from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from obmanage.management.deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentError,
    DeploymentRequest,
    DeploymentSelection,
    DeploymentTarget,
)
from obmanage.management.models import ManagementIssue, OpenedVaultCandidates, VaultInfo
from obmanage.pages.distribution import (
    DeploymentPreviewRow,
    ObsidianConfigPage,
    TemplateSuitePage,
    TemplaterPage,
)


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


def dispose(page) -> None:
    page.hide()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def snapshot_tree(root: Path) -> dict[str, tuple[str, bytes]]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            "dir" if path.is_dir() else "file",
            b"" if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    }


def run_last_operation(page, operations: list, starter):
    before = len(operations)
    starter()
    assert len(operations) == before + 1
    operation = operations[-1]
    result = operation(threading.Event(), lambda _event: None)
    page.task_finished("ok", result)
    for attribute in ("result", "plan", "catalog"):
        if hasattr(result, attribute):
            return getattr(result, attribute)
    return result


def closed_vault_reader(_config_path=None) -> OpenedVaultCandidates:
    return OpenedVaultCandidates()


def check_target(page, row: int = 0, checked: bool = True) -> None:
    index = page.target_model.index(row, 0)
    assert page.target_model.setData(
        index,
        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
        Qt.ItemDataRole.CheckStateRole,
    )


def create_config_fixture(tmp_path: Path):
    root = tmp_path / "仓库集合"
    source = root / "模板来源"
    target = root / "目标仓库"
    fake = root / "普通文件夹"
    source_config = source / ".obsidian"
    target_config = target / ".obsidian"
    source_config.mkdir(parents=True)
    target_config.mkdir(parents=True)
    fake.mkdir(parents=True)
    (source_config / "same.json").write_text("same", encoding="utf-8")
    (source_config / "changed.json").write_text("new", encoding="utf-8")
    (source_config / "added.css").write_text("added", encoding="utf-8")
    (target_config / "same.json").write_text("same", encoding="utf-8")
    (target_config / "changed.json").write_text("old", encoding="utf-8")
    (target_config / "target-only.json").write_text("remove", encoding="utf-8")
    return root, source, target, fake


def create_pending_config_batch(state: Path, source: Path, target: Path, target_id: str):
    source_config = source / ".obsidian"
    target_config = target / ".obsidian"
    source_config.mkdir(parents=True)
    target_config.mkdir(parents=True)
    (source_config / "value.json").write_text(f"new-{target_id}", encoding="utf-8")
    (target_config / "value.json").write_text(f"old-{target_id}", encoding="utf-8")
    request = DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source),
        DeploymentTarget(target_id, str(target)),
    ),), label="obmanage-ui:obsidian_config")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    assert engine.execute(plan).success
    return plan


def prepare_config_plan(page, operations: list) -> None:
    run_last_operation(page, operations, page._start_catalog)
    assert page.target_model.rowCount() == 1
    check_target(page)
    run_last_operation(page, operations, page._start_analyze)


def test_catalog_lists_only_real_vaults_excludes_source_and_analyze_is_read_only(
    application, tmp_path
):
    root, source, target, fake = create_config_fixture(tmp_path)
    state = tmp_path / "state"
    page = ObsidianConfigPage(
        state, {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    before = snapshot_tree(root)
    try:
        catalog = run_last_operation(page, operations, page._start_catalog)

        assert {item.path for item in catalog.vaults} == {str(source), str(target)}
        assert page.target_model.rowCount() == 1
        assert page.target_model.rows[0].path == str(target)
        assert fake.name not in {item.name for item in page.target_model.rows}
        assert page.target_model.checked_vaults() == ()
        assert page.target_model.data(
            page.target_model.index(0, 0), Qt.ItemDataRole.CheckStateRole
        ) == Qt.CheckState.Unchecked
        assert "排除 1 个来源仓库" in page.target_summary.text()

        check_target(page)
        assert page.plan is None
        plan = run_last_operation(page, operations, page._start_analyze)
        assert plan is page.plan
        assert not (state / "deployment-journal").exists()
        assert snapshot_tree(root) == before
    finally:
        dispose(page)


def test_file_preview_complete_clone_confirmation_copy_and_target_invalidation(
    application, tmp_path
):
    root, source, _target, _fake = create_config_fixture(tmp_path)
    page = ObsidianConfigPage(
        tmp_path / "state", {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        prepare_config_plan(page, operations)

        assert {row.action for row in page.preview_model.rows} == {
            "add", "update", "delete", "skip"
        }
        delete_rows = [row for row in page.preview_model.rows if row.action == "delete"]
        assert len(delete_rows) == 1
        assert delete_rows[0].absolute_path.endswith("target-only.json")
        index = page.preview_model.index(page.preview_model.rows.index(delete_rows[0]), 3)
        tooltip = page.preview_model.data(index, Qt.ItemDataRole.ToolTipRole)
        assert delete_rows[0].absolute_path in tooltip
        assert "删除 1" in page.preview_summary.text()
        assert "删除目标内 1 个文件" in page.confirm_checkbox.text()
        assert not page.execute_button.isEnabled()

        page.preview_table.selectRow(index.row())
        page.copy_selected_preview_paths()
        assert QApplication.clipboard().text() == delete_rows[0].absolute_path

        page.confirm_checkbox.setChecked(True)
        assert page.execute_button.isEnabled()
        check_target(page, checked=False)
        assert page.plan is None
        assert page.preview_model.rowCount() == 0
        assert not page.confirm_checkbox.isChecked()
        assert not page.execute_button.isEnabled()
        assert "删除：尚未分析" in page.preview_summary.text()
    finally:
        dispose(page)


def test_execute_and_new_page_can_rollback_persistent_batch(application, tmp_path):
    root, source, target, _fake = create_config_fixture(tmp_path)
    original = snapshot_tree(target / ".obsidian")
    settings = {"root": str(root), "source": str(source)}
    state = tmp_path / "state"
    page = ObsidianConfigPage(
        state, settings, str(root), opened_vault_reader=closed_vault_reader
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    recovered = None
    try:
        prepare_config_plan(page, operations)
        page.confirm_checkbox.setChecked(True)
        result = run_last_operation(page, operations, page._start_execute)

        assert result.success
        assert snapshot_tree(target / ".obsidian") == snapshot_tree(source / ".obsidian")
        assert page.current_batch is not None
        assert page.current_batch.status == "committed"
        assert page.rollback_button.isEnabled()
        assert page.finalize_confirm.isEnabled()
        assert not page.finalize_button.isEnabled()

        recovered = ObsidianConfigPage(
            state, settings, str(root), opened_vault_reader=closed_vault_reader
        )
        recovered_operations: list = []
        recovered.task_requested.connect(recovered_operations.append)
        assert recovered.current_batch is not None
        assert recovered.current_batch.batch_id == result.batch_id
        assert recovered.current_batch.status == "committed"

        rollback = run_last_operation(
            recovered, recovered_operations, recovered._start_rollback
        )
        assert rollback.success
        assert snapshot_tree(target / ".obsidian") == original
        assert recovered.current_batch is not None
        assert recovered.current_batch.status == "rolled_back"
        assert not recovered.rollback_button.isEnabled()
        assert not recovered.finalize_button.isEnabled()
    finally:
        if recovered is not None:
            dispose(recovered)
        dispose(page)


def test_execute_and_new_page_can_finalize_persistent_batch(application, tmp_path):
    root, source, target, _fake = create_config_fixture(tmp_path)
    settings = {"root": str(root), "source": str(source)}
    state = tmp_path / "state"
    page = ObsidianConfigPage(
        state, settings, str(root), opened_vault_reader=closed_vault_reader
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    recovered = None
    try:
        prepare_config_plan(page, operations)
        page.confirm_checkbox.setChecked(True)
        result = run_last_operation(page, operations, page._start_execute)
        assert result.success
        assert tuple(target.glob(".obmanage-deploy-*.backup"))

        recovered = ObsidianConfigPage(
            state, settings, str(root), opened_vault_reader=closed_vault_reader
        )
        recovered_operations: list = []
        recovered.task_requested.connect(recovered_operations.append)
        before = len(recovered_operations)
        recovered._start_finalize()
        assert len(recovered_operations) == before
        recovered.finalize_confirm.setChecked(True)
        assert recovered.finalize_button.isEnabled()
        recovered.invalidate_confirmation()
        assert not recovered.finalize_confirm.isChecked()
        assert not recovered.finalize_button.isEnabled()
        recovered.finalize_confirm.setChecked(True)
        finalized = run_last_operation(
            recovered, recovered_operations, recovered._start_finalize
        )

        assert finalized.success
        assert snapshot_tree(target / ".obsidian") == snapshot_tree(source / ".obsidian")
        assert not tuple(target.glob(".obmanage-deploy-*.backup"))
        assert recovered.current_batch is not None
        assert recovered.current_batch.status == "finalized"
        assert not recovered.has_pending_recovery()
        assert not recovered.finalize_confirm.isChecked()
    finally:
        if recovered is not None:
            dispose(recovered)
        dispose(page)


def test_all_pending_deployment_batches_remain_selectable_and_actions_use_selection(
    application, tmp_path
):
    state = tmp_path / "state"
    root = tmp_path / "目标集合"
    first_target = root / "早期目标"
    second_target = root / "最新目标"
    first = create_pending_config_batch(
        state, tmp_path / "早期来源", first_target, "first"
    )
    # New versions refuse to create another deployment while one is pending.
    # Temporarily mark the first record complete, create the second, then put
    # the first authenticated record back into its legacy pending state to
    # model multiple batches left by an older release/crash history.
    journal = DeploymentEngine(state).journal
    first_target_record = journal.get(first.batch_id).targets[0]
    journal.set_batch(first.batch_id, status="finalizing")
    journal.set_target(first.batch_id, first_target_record.selection_id, phase="finalized")
    journal.set_batch(first.batch_id, status="finalized")
    second = create_pending_config_batch(
        state, tmp_path / "最新来源", second_target, "second"
    )
    journal.set_batch(first.batch_id, status="finalizing")
    journal.set_target(first.batch_id, first_target_record.selection_id, phase="committed")
    journal.set_batch(first.batch_id, status="committed")
    page = ObsidianConfigPage(
        state, {"root": str(root), "source": str(tmp_path / "最新来源")}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        assert page.has_pending_recovery()
        assert page.batch_selector.count() == 2
        assert {
            page.batch_selector.itemData(index)
            for index in range(page.batch_selector.count())
        } == {first.batch_id, second.batch_id}

        page.finalize_confirm.setChecked(True)
        older_index = page.batch_selector.findData(first.batch_id)
        assert older_index >= 0
        page.batch_selector.setCurrentIndex(older_index)
        assert page.current_batch is not None
        assert page.current_batch.batch_id == first.batch_id
        assert not page.finalize_confirm.isChecked()

        rolled_back = run_last_operation(page, operations, page._start_rollback)

        assert rolled_back.success
        assert (first_target / ".obsidian" / "value.json").read_text(
            encoding="utf-8"
        ) == "old-first"
        assert (second_target / ".obsidian" / "value.json").read_text(
            encoding="utf-8"
        ) == "new-second"
        assert page.batch_selector.count() == 1
        assert page.batch_selector.currentData() == second.batch_id
        assert page.current_batch is not None
        assert page.current_batch.batch_id == second.batch_id
        assert page.has_pending_recovery()
    finally:
        dispose(page)


def test_unreadable_persistent_authority_blocks_recovery_ui(application, tmp_path):
    root = tmp_path / "仓库集合"
    source = root / "来源"
    source.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    (state / "deployment-journal.key").write_bytes(b"partial")

    page = ObsidianConfigPage(
        state, {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    try:
        assert page._recovery_blocked
        assert page.has_pending_recovery()
        assert page.batch_selector.count() == 0
        assert "无法读取持久批次" in page.batch_status.text()
        assert not page.rollback_button.isEnabled()
        assert not page.finalize_button.isEnabled()
    finally:
        dispose(page)


def test_unknown_legacy_batch_is_reachable_from_config_recovery_page(
        application, tmp_path):
    root = tmp_path / "仓库集合"
    source = root / "旧版来源"
    target = root / "目标"
    (source / ".obsidian").mkdir(parents=True)
    (target / ".obsidian").mkdir(parents=True)
    (source / ".obsidian" / "value.json").write_text("new", encoding="utf-8")
    (target / ".obsidian" / "value.json").write_text("old", encoding="utf-8")
    state = tmp_path / "state"
    request = DeploymentRequest((DeploymentSelection(
        DeploymentComponent.obsidian(source),
        DeploymentTarget("legacy-target", str(target)),
    ),), label="legacy-distribution-v0")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    assert engine.execute(plan).success

    suite = TemplateSuitePage(
        state, {"root": str(root), "source": ""}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    config = ObsidianConfigPage(
        state, {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    operations: list = []
    config.task_requested.connect(operations.append)
    try:
        assert not suite.has_pending_recovery()
        assert config.has_pending_recovery()
        assert config.batch_selector.count() == 1
        assert config.batch_selector.currentData() == plan.batch_id
        assert "旧版/未知入口" in config.batch_selector.currentText()
        assert config.current_batch is not None
        assert config.current_batch.label == "legacy-distribution-v0"
        assert str(target / ".obsidian") in config.batch_status.toolTip()
        assert config.rollback_button.isEnabled()

        result = run_last_operation(config, operations, config._start_rollback)

        assert result.success
        assert not config.has_pending_recovery()
        assert (target / ".obsidian" / "value.json").read_text(
            encoding="utf-8"
        ) == "old"
    finally:
        dispose(config)
        dispose(suite)


def test_completed_rollback_with_residuals_remains_visible(application, tmp_path):
    root = tmp_path / "仓库集合"
    target = root / "目标"
    target.mkdir(parents=True)
    source = tmp_path / "来源"
    source.mkdir()
    (source / "value.json").write_text("new", encoding="utf-8")
    state = tmp_path / "state"
    component = DeploymentComponent.direct("nested", source, "A/B/Config")
    request = DeploymentRequest((DeploymentSelection(
        component, DeploymentTarget("target", str(target)),
    ),), label="obmanage-ui:obsidian_config")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    engine.journal.set_target(
        plan.batch_id, record.selection_id,
        created_parents=(record.created_parents[0],),
    )
    assert engine.rollback(plan.batch_id).journal_status == "rolled_back_with_residuals"

    page = ObsidianConfigPage(
        state, {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    try:
        assert not page.has_pending_recovery()
        assert page.batch_selector.count() == 1
        assert page.current_batch is not None
        assert page.current_batch.status == "rolled_back_with_residuals"
        assert "残留目录" in page.batch_status.text()
        assert "已保留" in page.batch_status.toolTip()
        assert not page.rollback_button.isEnabled()
        assert not page.finalize_button.isEnabled()
    finally:
        dispose(page)


def test_suite_and_templater_build_explicit_bounded_requests_and_restore_settings(
    application, tmp_path
):
    root = tmp_path / "仓库集合"
    target = root / "目标"
    (target / ".obsidian").mkdir(parents=True)
    suite_source = tmp_path / "模板套件"
    (suite_source / ".claude").mkdir(parents=True)
    (suite_source / ".claude" / "rules.md").write_text("rule", encoding="utf-8")
    suite = TemplateSuitePage(
        tmp_path / "suite-state",
        {"root": str(root), "source": str(suite_source), "components": []},
        str(root),
        opened_vault_reader=closed_vault_reader,
    )
    templater_source = tmp_path / "来源" / "File" / "Templater"
    templater_source.mkdir(parents=True)
    (templater_source / "note.md").write_text("template", encoding="utf-8")
    templater = TemplaterPage(
        tmp_path / "templater-state",
        {"root": str(root), "source": str(templater_source)},
        str(root),
        opened_vault_reader=closed_vault_reader,
    )
    try:
        assert suite.repository_paths() == (str(root), str(suite_source))
        assert templater.repository_paths() == (str(root), str(templater_source))
        assert suite.selected_component_ids() == ()
        suite.target_model.set_rows((VaultInfo(str(target), target.name),))
        assert suite.target_model.checked_vaults() == ()
        suite.component_boxes["claude"].setChecked(True)
        check_target(suite)
        suite_request = suite._build_request()
        assert len(suite_request.selections) == 1
        assert suite_request.selections[0].component.destination == ".claude"
        assert Path(suite_request.selections[0].component.source) == suite_source / ".claude"

        assert templater.selected_component_ids() == ("templater",)
        templater.target_model.set_rows((VaultInfo(str(target), target.name),))
        check_target(templater)
        templater_request = templater._build_request()
        assert len(templater_request.selections) == 1
        component = templater_request.selections[0].component
        assert component.destination == "File/Templater"
        assert component.source_subtree == "File/Templater"

        payload = suite.settings_payload()
        assert payload == {
            "root": str(root),
            "source": str(suite_source),
            "components": ["claude"],
        }
        suite.restore_settings({
            "root": str(root / "新范围"),
            "source": str(suite_source),
            "components": ["file", "obsidian"],
        })
        assert suite.selected_component_ids() == ("obsidian", "file")
        assert suite.target_model.checked_vaults() == ()

        suite.preview_model.set_rows((DeploymentPreviewRow(
            "delete", "file", target.name, str(target), str(target / "File"), 0,
            "目标独有目录", kind="dir",
        ),))
        assert "文件夹" in suite.preview_model.data(
            suite.preview_model.index(0, 0), Qt.ItemDataRole.DisplayRole
        )

        suite.resize(816, 620)
        suite.show()
        application.processEvents()
        assert suite.minimumSizeHint().width() <= 780
    finally:
        dispose(templater)
        dispose(suite)


def test_input_changes_clear_catalog_plan_and_confirmation(application, tmp_path):
    root, source, _target, _fake = create_config_fixture(tmp_path)
    page = ObsidianConfigPage(
        tmp_path / "state", {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        prepare_config_plan(page, operations)
        page.confirm_checkbox.setChecked(True)
        page.source_picker.set_value(str(source / ".obsidian"))

        assert page.catalog is None
        assert page.target_model.rowCount() == 0
        assert page.plan is None
        assert not page.confirm_checkbox.isChecked()
        assert not page.execute_button.isEnabled()
    finally:
        dispose(page)


@pytest.mark.parametrize("page_type", [ObsidianConfigPage, TemplaterPage])
def test_opened_source_candidates_are_lazy_revalidated_and_user_selected(
    application, tmp_path, page_type
):
    root = tmp_path / "目标集合"
    target = root / "目标"
    (target / ".obsidian").mkdir(parents=True)
    opened = tmp_path / "已打开来源"
    (opened / ".obsidian").mkdir(parents=True)
    invalid = tmp_path / "不是仓库"
    invalid.mkdir()
    calls: list[object] = []

    def reader(config_path):
        calls.append(config_path)
        return OpenedVaultCandidates(paths=(str(opened), str(invalid)))

    sentinel_config = tmp_path / "obsidian.json"
    page = page_type(
        tmp_path / f"state-{page_type.page_key}",
        {"root": str(root), "source": ""},
        str(root),
        obsidian_config_path=sentinel_config,
        opened_vault_reader=reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        assert calls == []
        assert page.opened_source_button is not None
        page._start_opened_sources()
        assert calls == []  # Reading happens only when the emitted worker operation runs.
        assert len(operations) == 1
        state = operations[-1](threading.Event(), lambda _event: None)
        page.task_finished("ok", state)

        assert calls == [sentinel_config]
        assert tuple(vault.path for vault in state.vaults) == (str(opened),)
        assert state.issues
        assert page.opened_source_menu is not None
        assert len(page.opened_source_menu.actions()) == 1
        page.opened_source_menu.actions()[0].trigger()
        assert page.source_picker.value == str(opened)
    finally:
        dispose(page)


def test_target_select_all_and_clear_are_explicit_and_revoke_plan(application, tmp_path):
    root = tmp_path / "集合"
    first = root / "一"
    second = root / "二"
    for vault in (first, second):
        (vault / ".obsidian").mkdir(parents=True)
    source = tmp_path / "来源"
    (source / ".obsidian").mkdir(parents=True)
    (source / ".obsidian" / "config.json").write_text("new", encoding="utf-8")
    page = ObsidianConfigPage(
        tmp_path / "state", {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=closed_vault_reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        run_last_operation(page, operations, page._start_catalog)
        assert page.target_model.checked_vaults() == ()
        page.select_all_targets_button.click()
        assert {item.path for item in page.target_model.checked_vaults()} == {
            str(first), str(second)
        }
        run_last_operation(page, operations, page._start_analyze)
        page.confirm_checkbox.setChecked(True)
        assert page.plan is not None

        page.clear_targets_button.click()
        assert page.target_model.checked_vaults() == ()
        assert page.plan is None
        assert not page.confirm_checkbox.isChecked()
    finally:
        dispose(page)


def test_open_target_is_blocked_again_in_worker_immediately_before_execute(
    application, tmp_path
):
    root, source, target, _fake = create_config_fixture(tmp_path)
    state_dir = tmp_path / "state"
    open_registry = {"paths": (str(target),)}
    reads: list[tuple[str, ...]] = []

    def reader(_config_path):
        paths = open_registry["paths"]
        reads.append(paths)
        return OpenedVaultCandidates(paths=paths)

    page = ObsidianConfigPage(
        state_dir, {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    target_before = snapshot_tree(target / ".obsidian")
    try:
        prepare_config_plan(page, operations)
        assert page.plan is not None
        assert not page.confirm_checkbox.isEnabled()
        assert not page.execute_button.isEnabled()
        assert "已阻止" in page.occupancy_status.text()

        open_registry["paths"] = ()
        run_last_operation(page, operations, page._start_analyze)
        assert page.confirm_checkbox.isEnabled()
        page.confirm_checkbox.setChecked(True)
        assert page.execute_button.isEnabled()

        # Occupancy can change after analysis; the worker must read it again.
        open_registry["paths"] = (str(target),)
        before = len(operations)
        page._start_execute()
        assert len(operations) == before + 1
        with pytest.raises(DeploymentError, match="标记为打开") as error:
            operations[-1](threading.Event(), lambda _event: None)
        page.task_finished("error", str(error.value))

        assert snapshot_tree(target / ".obsidian") == target_before
        assert not (state_dir / "deployment-journal").exists()
        assert page.plan is None
        assert "标记为打开" in page.status_label.text()
        assert len(reads) == 4  # catalog, first analyze, re-analyze, execute preflight
    finally:
        dispose(page)


def test_unreadable_open_registry_requires_explicit_closed_target_confirmation(
    application, tmp_path
):
    root, source, target, _fake = create_config_fixture(tmp_path)
    unavailable = OpenedVaultCandidates(issues=(ManagementIssue(
        "config_unreadable", "测试配置不可读取", str(tmp_path / "obsidian.json")
    ),))

    def reader(_config_path):
        return unavailable

    page = ObsidianConfigPage(
        tmp_path / "state", {"root": str(root), "source": str(source)}, str(root),
        opened_vault_reader=reader,
    )
    operations: list = []
    page.task_requested.connect(operations.append)
    try:
        prepare_config_plan(page, operations)
        assert page.open_state.reliable is False
        assert not page.occupancy_confirm.isHidden()
        assert "测试配置不可读取" in page.occupancy_status.text()

        page.confirm_checkbox.setChecked(True)
        assert not page.execute_button.isEnabled()
        page.occupancy_confirm.setChecked(True)
        assert page.execute_button.isEnabled()
        result = run_last_operation(page, operations, page._start_execute)

        assert result.success
        assert snapshot_tree(target / ".obsidian") == snapshot_tree(source / ".obsidian")
    finally:
        dispose(page)
