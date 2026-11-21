from __future__ import annotations

import os
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from obmanage.management import comsync_targets
from obmanage.management.comsync import DatedVault
from obmanage.management.models import ManagementIssue, VaultCatalogResult, VaultInfo
from obmanage.management.running_vaults import RuntimeSnapshot, UnresolvedWindow, VaultSession, vault_identity
from obmanage.pages import distribution
from obmanage.pages.distribution import ComSyncPage, TargetScope


def make_vault(path: Path) -> Path:
    (path / ".obsidian").mkdir(parents=True)
    return path


@pytest.fixture
def page_setup(tmp_path):
    app = QApplication.instance() or QApplication([])
    collection = tmp_path / "collection"
    source = make_vault(collection / "source")
    vaults = {name: make_vault(collection / name) for name in ("old", "new", "tie")}
    page = ComSyncPage(tmp_path / "state", {"source": str(source)}, str(collection))
    operations = []
    page.task_requested.connect(operations.append)
    yield page, operations, collection, source, vaults
    page.deleteLater()
    app.processEvents()


def finish_task(page, operations):
    operation = operations.pop(0)
    result = operation(Event(), lambda _progress: None)
    page.task_finished("ok", result)
    return result


def refresh(page, operations):
    page.catalog_button.click()
    return finish_task(page, operations)


def visible_names(page) -> set[str]:
    return {vault.name for vault in page.target_model.rows}


def selected_names(page) -> set[str]:
    return {vault.name for vault in page.target_model.checked_vaults()}


def check_target(page, name):
    row = next(i for i, vault in enumerate(page.target_model.rows) if vault.name == name)
    assert page.target_model.setData(page.target_model.index(row, 0),
                                    Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)


def test_running_scope_keeps_root_and_freezes_only_manually_checked_target(tmp_path, page_setup, monkeypatch):
    page, operations, collection, source, vaults = page_setup
    nested = make_vault(source / "nested")
    elsewhere = make_vault(tmp_path / "outside")
    runtime = [*vaults.values(), source, nested, elsewhere]
    monkeypatch.setattr(distribution, "read_runtime_snapshot", lambda *args, **kwargs:
                        RuntimeSnapshot(tuple(VaultSession(VaultInfo(str(vault), vault.name),
                                              vault_identity(str(vault)), ()) for vault in runtime)))
    saved = page.settings_payload()
    refresh(page, operations)
    assert page.target_scope_buttons.button(TargetScope.ALL).isChecked()
    assert page.root_picker.edit.isReadOnly()
    assert not hasattr(page.root_picker, "running_vault")
    assert visible_names(page) == {"old", "new", "tie"}
    assert not selected_names(page)
    page.target_scope_buttons.button(TargetScope.RUNNING).click()
    assert all(not button.isEnabled() for button in page.target_scope_buttons.buttons())
    finish_task(page, operations)
    assert visible_names(page) == {"old", "new", "tie"}
    assert not selected_names(page)
    check_target(page, "new")
    request = page._freeze_request()
    assert [target.binding.path for target in request.selected_targets] == [str(vaults["new"])]
    assert request.collection.path == str(collection)
    assert request.collection.contains(request.selected_targets[0].binding)
    runtime.remove(vaults["new"])
    page.target_scope_buttons.button(TargetScope.RUNNING).click()
    finish_task(page, operations)
    assert selected_names(page) == set()
    assert [target.binding.path for target in request.selected_targets] == [str(vaults["new"])]
    assert page.settings_payload() == saved


def test_latest_scope_computes_ties_before_search_and_drops_invisible_selection(page_setup, monkeypatch):
    page, operations, _, _, _ = page_setup
    dates = {"old": 10, "new": 20, "tie": 20, "source": 30}
    monkeypatch.setattr(comsync_targets, "with_creation_times", lambda catalog, _cancel:
                        VaultCatalogResult(tuple(DatedVault(v.path, v.name, v.parent_path, dates[v.name])
                                                 for v in catalog.vaults), catalog.issues))
    settings = page.settings_payload()
    refresh(page, operations)
    page.target_filter.setText("old")
    page.select_all_targets_button.click()
    assert selected_names(page) == {"old"}
    page.plan = object()
    page.target_scope_buttons.button(TargetScope.LATEST).click()
    assert page.target_filter.text() == "old"
    assert not visible_names(page)
    assert not selected_names(page)
    assert page.plan is None
    assert "可见 0 · 有效 3 · 已选 0" in page.target_summary.text()
    assert "已移除 1 个" in page.target_summary.text()
    page.target_filter.clear()
    assert visible_names(page) == {"new", "tie"}
    assert not selected_names(page)
    page.select_all_targets_button.click()
    assert selected_names(page) == {"new", "tie"}
    page.plan = object()
    keep = page.plan
    page.target_table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert page.plan is keep
    assert selected_names(page) == {"new", "tie"}
    page.target_filter.setText(os.sep + "tie")
    assert selected_names(page) == {"tie"}
    assert page.plan is None
    path_index = page.target_proxy.index(0, 2)
    path = path_index.data(Qt.ItemDataRole.ToolTipRole)
    page.target_table.selectRow(0)
    page.copy_selected_target_paths()
    assert QApplication.clipboard().text() == path
    page.target_filter.clear()
    page.target_scope_buttons.button(TargetScope.ALL).click()
    assert selected_names(page) == {"tie"}
    page.clear_targets_button.click()
    assert not selected_names(page)
    assert page.settings_payload() == settings


def test_refresh_preserves_only_same_directory_identity_and_never_selects_new_rows(tmp_path, page_setup):
    page, operations, collection, source, vaults = page_setup
    refresh(page, operations)
    page.select_all_targets_button.click()
    page.plan = object()
    keep = page.plan
    refresh(page, operations)
    assert page.plan is keep
    assert selected_names(page) == set(vaults)
    vaults["old"].rename(tmp_path / "original")
    make_vault(vaults["old"])
    make_vault(collection / "added")
    refresh(page, operations)
    assert selected_names(page) == {"new", "tie"}
    assert visible_names(page) == {"old", "new", "tie", "added"}
    assert page.plan is None
    assert "已移除 1 个" in page.target_summary.text()
    page.source_picker.set_value(str(source / ".obsidian"))
    assert visible_names(page) == {"old", "new", "tie", "added"}
    assert not selected_names(page)
    saved = page.settings_payload()
    assert all("target" not in key for key in saved)
    page.restore_settings(saved)
    assert not visible_names(page) and not selected_names(page)


def test_late_catalog_is_discarded_after_collection_changes(tmp_path, page_setup):
    page, operations, _, _, _ = page_setup
    page.catalog_button.click()
    other = make_vault(tmp_path / "other")
    page.root_picker.set_value(str(other))
    finish_task(page, operations)
    assert page.catalog is None
    assert not visible_names(page)
    refresh(page, operations)
    assert visible_names(page) == {"other"}
    assert page.settings_payload()["root"] == str(other)


def test_running_scope_excludes_window_for_replaced_candidate(tmp_path, page_setup, monkeypatch):
    page, operations, _, _, vaults = page_setup
    refresh(page, operations)
    check_target(page, "old")
    previous = page._catalog_bindings[str(vaults["old"])]
    vaults["old"].rename(tmp_path / "original")
    make_vault(vaults["old"])
    assert not previous.matches()
    replacement = VaultSession(VaultInfo(str(vaults["old"]), "old"), vault_identity(str(vaults["old"])), ())
    monkeypatch.setattr(distribution, "read_runtime_snapshot", lambda *args, **kwargs:
                        RuntimeSnapshot((replacement,)))
    page.target_scope_buttons.button(TargetScope.RUNNING).click()
    finish_task(page, operations)
    assert not visible_names(page)
    assert not selected_names(page)
    assert "目录身份已改变" in page.target_scope_status.text()
    refresh(page, operations)
    assert visible_names(page) == {"old"}
    assert not selected_names(page)


def test_unavailable_running_and_creation_evidence_are_reported(page_setup, monkeypatch):
    page, operations, _, _, _ = page_setup
    monkeypatch.setattr(comsync_targets, "with_creation_times", lambda catalog, _cancel: catalog)
    monkeypatch.setattr(distribution, "read_runtime_snapshot", lambda *args, **kwargs:
                        RuntimeSnapshot(unresolved=(UnresolvedWindow(
                            ManagementIssue("runtime_unavailable", "访问被拒绝")),)))
    refresh(page, operations)
    page.target_scope_buttons.button(TargetScope.LATEST).click()
    assert not visible_names(page)
    assert "创建时间不可读取" in page.target_scope_status.text()
    page.target_scope_buttons.button(TargetScope.RUNNING).click()
    finish_task(page, operations)
    assert not visible_names(page)
    assert page.target_scope_status.text() == "运行识别不完整：访问被拒绝"
