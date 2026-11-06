"""Shared picker task adapter, using the application's existing single task slot."""
from __future__ import annotations

import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal

from ..management.catalog import discover_vaults, read_registered_vault_candidates
from ..management.models import ManagementIssue, VaultCatalogResult
from ..management.running_vaults import read_running_vaults
from ..models import SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, identity, snapshot
from ..scanning.metrics import debug_metrics, log_metrics
from .vault_chooser import VaultSourceDialog


@dataclass(frozen=True)
class VaultChoices:
    choices: VaultCatalogResult
    collection: VaultCatalogResult | None = None
    display: CatalogDisplay | None = None


@dataclass(frozen=True)
class CatalogDisplay:
    """Historical rows and identity hints, never a business catalog or plan."""

    rows: VaultCatalogResult
    scope_identity: tuple
    bindings: tuple


class CatalogPresentationCache:
    """Small GUI-thread LRU shared by the selectors in one window."""

    def __init__(self):
        self._entries = OrderedDict()
        self.generation = 0

    def get(self, key):
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def remember(self, key, display):
        self._entries[key] = display
        self._entries.move_to_end(key)
        if len(self._entries) > 8:
            self._entries.popitem(last=False)

    def invalidate(self):
        self._entries.clear()
        self.generation += 1


def _scope_identity(roots, config):
    states = []
    for path in roots:
        try:
            state = assert_plain_chain(path)
            states.append(identity(state) if state and state["inode"] else None)
        except (OSError, SyncError):
            states.append(None)
    state = None
    try:
        if config:
            assert_plain_chain(os.path.dirname(config))
            state = snapshot(config)
    except (OSError, SyncError):
        pass
    return tuple(states), tuple(state.items()) if state else None


def _directory_binding(path):
    root = assert_plain_chain(path)
    marker = snapshot(os.path.join(path, ".obsidian"))
    if (root is None or marker is None or marker["kind"] != "dir"
            or not root["inode"] or not marker["inode"]):
        raise SyncError("无法确认仓库目录和 .obsidian 标记身份。")
    return identity(root), identity(marker)


class VaultSelection(QObject):
    task_requested = Signal(object)
    status_changed = Signal(str, str)
    catalog_ready = Signal(object)
    cancel_requested = Signal()
    preview_stale = Signal()

    def __init__(self, parent, accept, browse, roots=lambda: ()):
        super().__init__(parent)
        self.accept = accept
        self.browse = browse
        self.roots = roots
        self.config_path = None
        self.catalog_transform = None
        self.cache = CatalogPresentationCache()
        self.dialog = None
        self._task_active = False
        self._dismissed = False
        self._pending_path = None
        self._history = None
        self._query = None
        self._metrics = None
        self.running = False

    def _scope(self):
        roots = {os.path.normcase(path): path for path in map(canonical, filter(None, self.roots()))}
        appdata = os.environ.get("APPDATA")
        config = self.config_path or (Path(appdata) / "obsidian" / "obsidian.json" if appdata else None)
        return tuple(roots[key] for key in sorted(roots)), canonical(config) if config else None, self.catalog_transform

    def _key(self, scope):
        roots, config, transform = scope
        return (tuple(os.path.normcase(root) for root in roots),
                os.path.normcase(config) if config else None,
                "registered_exact_and_collection_recursive", transform)

    def start(self, running=False):
        if self._task_active:
            return
        self.dismiss()
        self._dismissed = False
        self._pending_path = None
        self._metrics = debug_metrics("vault_picker")
        if self._metrics is not None:
            self._started = time.perf_counter()
        self.running = running
        scope = self._scope()
        roots, config, transform = scope
        self._query = self._key(scope)
        self._generation = self.cache.generation
        self._history = None if running else self.cache.get(self._query)

        def operation(cancel, progress):
            if cancel.is_set():
                raise SyncCancelled("已取消仓库选择。")
            if running:
                return VaultChoices(read_running_vaults(config, cancel=cancel))
            before = _scope_identity(roots, config)
            registered = read_registered_vault_candidates(config)
            exact = discover_vaults(registered.paths, recursive=False, cancel=cancel, progress=progress)
            collection = discover_vaults(roots, cancel=cancel, progress=progress) if roots else VaultCatalogResult()
            if transform is not None:
                collection = transform(collection, cancel)
            vaults = {vault.path: vault for vault in (*exact.vaults, *collection.vaults)}
            valid_paths = {vault.path for vault in exact.vaults}
            invalid = tuple(ManagementIssue("registered_vault_invalid",
                            "已跳过不可用或没有 .obsidian 的登记仓库。", path, "warning")
                            for path in registered.paths if path not in valid_paths)
            issues = [*registered.issues, *exact.issues, *collection.issues, *invalid]
            visible = []
            bindings = []
            for vault in sorted(vaults.values(), key=lambda v: (v.name.casefold(), v.path.casefold())):
                if cancel.is_set():
                    raise SyncCancelled("已取消仓库选择。")
                try:
                    binding = _directory_binding(vault.path)
                except (OSError, SyncError):
                    issues.append(ManagementIssue("picker_vault_changed",
                        "仓库或 .obsidian 在检查期间不可用。", vault.path))
                else:
                    bindings.append((vault.path, binding))
                    visible.append(vault)
            after = _scope_identity(roots, config)
            if after != before:
                issues.append(ManagementIssue("picker_scope_changed",
                    "搜索范围或 Obsidian 登记在读取期间发生变化，请重新打开列表。"))
                visible.clear()
            if cancel.is_set():
                raise SyncCancelled("已取消仓库选择。")
            choices = VaultCatalogResult(tuple(visible), tuple(issues))
            scope_identity = after, tuple(os.path.normcase(path) for path in registered.paths)
            display = (CatalogDisplay(choices, scope_identity, tuple(bindings))
                       if not issues and None not in after[0] and after[1] else None)
            return VaultChoices(choices, collection, display)

        self._task_active = True
        self.set_status("正在识别运行仓库…" if running else "正在读取仓库列表…", "busy")
        self.task_requested.emit(operation)
        if self._task_active and self._history is not None and self._history.rows.vaults:
            self._open_dialog(self._history.rows, "上次扫描结果 · 正在实时刷新", deferred=True)

    def dismiss(self):
        self._dismissed = True
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    def _open_dialog(self, rows, label="", *, deferred=False):
        dialog = self.dialog = VaultSourceDialog(rows, self.parent(), running=self.running)
        dialog.source_chosen.connect(self._choose_path)
        dialog.browse_requested.connect(self.browse)
        dialog.finished.connect(self._dialog_finished)
        if self._metrics is not None:
            dialog.first_painted.connect(self._first_painted)
        dialog.set_freshness(label, deferred=deferred)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.open()

    def _dialog_finished(self, _result):
        if self._task_active and not self._dismissed:
            self._dismissed = True
            self.cancel_requested.emit()

    def _first_painted(self):
        if self._metrics is not None:
            self._metrics.elapsed("ui_first_visible", time.perf_counter() - self._started)
            if self._metrics.status != "running":
                log_metrics(self._metrics)
                self._metrics = None

    def _matches_query(self):
        return self._query == self._key(self._scope()) and self._generation == self.cache.generation

    def _choose_path(self, path):
        if not self._matches_query():
            self.set_status("仓库范围已改变，请重新打开列表。", "warning")
            return
        if self._task_active:
            self._pending_path = path
            self.dialog.set_freshness("上次扫描结果 · 正在核验所选仓库…",
                                      selectable=False, deferred=True)
        else:
            self.accept(path)

    def task_cancellable(self):
        return self._task_active

    def refresh_actions(self):
        pass

    def set_status(self, text, kind="neutral"):
        self.status_changed.emit(text, kind)

    def task_progress(self, event):
        self.set_status(event.message or "正在读取仓库…", "busy")

    def task_finished(self, status, payload):
        self._task_active = False
        history, self._history = self._history, None
        if self._dismissed or not self._matches_query():
            if not self._dismissed:
                self.set_status("仓库范围或事务状态已改变，请重新打开列表。", "warning")
            self.dismiss()
            self._finish_metrics("dismissed")
            return
        if status != "ok":
            message = str(payload) if status == "error" else "已取消仓库选择。"
            if status == "error" and history is not None:
                self.preview_stale.emit()
            if self.dialog is not None:
                reason = "刷新失败" if status == "error" else "刷新已取消"
                self.dialog.set_freshness(f"上次扫描结果 · {reason}，请重新打开列表。", selectable=False)
                if status == "error":
                    self.dialog.freshness.setToolTip(message)
            self.set_status(message, "warning")
            self._finish_metrics(status)
            return
        display, rows = payload.display, payload.choices
        if history is not None and (display is None or history.scope_identity != display.scope_identity
                                    or history.bindings != display.bindings):
            self.preview_stale.emit()
        if payload.collection is not None and not any(
                issue.code == "picker_scope_changed" for issue in payload.choices.issues):
            self.catalog_ready.emit(payload.collection)
        # One unambiguous live vault can be filled by the explicit recognition action.
        if self.running and len(rows.vaults) == 1 and not rows.issues:
            self.accept(rows.vaults[0].path)
            self.set_status("已识别当前运行仓库，请核对路径。", "success")
            self._finish_metrics("success")
            return
        if display is not None:
            self.cache.remember(self._query, display)
        label = ("本次扫描有读取问题" if rows.issues else "本次扫描结果 · 已实时确认") if not self.running else ""
        if self.dialog is None:
            self._open_dialog(rows, label)
        else:
            selected = self._pending_path or self.dialog.selected_path()
            same = False
            if selected is not None and display is not None:
                previous = dict(history.bindings).get(selected)
                same = (previous is not None and dict(display.bindings).get(selected) == previous
                        and history.scope_identity == display.scope_identity)
            self.dialog.set_result(rows, selected if same else None)
            if selected and not same:
                label += " · 所选仓库或范围未能核验，请重新选择。"
            self.dialog.set_freshness(label)
            if same and self._pending_path:
                self.accept(selected)
                self.dialog.accept()
        self.set_status(f"发现 {len(rows.vaults)} 个仓库，请选择。" if rows.vaults else
                        (rows.issues[0].message if rows.issues else "未发现仓库，请浏览选择。"),
                        "warning" if rows.issues else "neutral")
        self._finish_metrics("incomplete" if rows.issues else "success")

    def _finish_metrics(self, status):
        if self._metrics is not None:
            self._metrics.status = status
            if status == "success":
                self._metrics.elapsed("ui_verified_ready", time.perf_counter() - self._started)
            if ("ui_first_visible" in self._metrics.seconds or self.dialog is None
                    or not self.dialog.isVisible() or not self.dialog.model.rowCount()):
                log_metrics(self._metrics)
                self._metrics = None
