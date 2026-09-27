"""Shared picker task adapter, using the application's existing single task slot."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal

from ..management.catalog import discover_vaults, read_registered_vault_candidates
from ..management.models import ManagementIssue, VaultCatalogResult
from ..management.running_vaults import read_running_vaults
from ..models import SyncCancelled
from .vault_chooser import VaultSourceDialog


@dataclass(frozen=True)
class VaultChoices:
    choices: VaultCatalogResult
    collection: VaultCatalogResult | None = None


class VaultSelection(QObject):
    task_requested = Signal(object)
    status_changed = Signal(str, str)
    catalog_ready = Signal(object)

    def __init__(self, parent, accept, browse, roots=lambda: ()):
        super().__init__(parent)
        self.accept = accept
        self.browse = browse
        self.roots = roots
        self.config_path = None
        self.dialog = None
        self._task_active = False
        self.running = False

    def start(self, running=False):
        if self._task_active:
            return
        self.running = running
        roots = tuple(root for root in self.roots() if root)
        config = self.config_path

        def operation(cancel, progress):
            if cancel.is_set():
                raise SyncCancelled("已取消仓库选择。")
            if running:
                return VaultChoices(read_running_vaults(config, cancel=cancel))
            registered = read_registered_vault_candidates(config)
            exact = discover_vaults(registered.paths, recursive=False, cancel=cancel, progress=progress)
            collection = discover_vaults(roots, cancel=cancel, progress=progress) if roots else VaultCatalogResult()
            vaults = {vault.path: vault for vault in (*exact.vaults, *collection.vaults)}
            valid_paths = {vault.path for vault in exact.vaults}
            invalid = tuple(ManagementIssue("registered_vault_invalid",
                            "已跳过不可用或没有 .obsidian 的登记仓库。", path, "warning")
                            for path in registered.paths if path not in valid_paths)
            return VaultChoices(VaultCatalogResult(
                tuple(sorted(vaults.values(), key=lambda v: (v.name.casefold(), v.path.casefold()))),
                (*registered.issues, *exact.issues, *collection.issues, *invalid)), collection)

        self._task_active = True
        self.set_status("正在识别运行仓库…" if running else "正在读取仓库列表…", "busy")
        self.task_requested.emit(operation)

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
        if status != "ok":
            self.set_status(str(payload) if status == "error" else "已取消仓库选择。", "warning")
            return
        if payload.collection is not None:
            self.catalog_ready.emit(payload.collection)
        payload = payload.choices
        # One unambiguous live vault can be filled by the explicit recognition action.
        if self.running and len(payload.vaults) == 1 and not payload.issues:
            self.accept(payload.vaults[0].path)
            self.set_status("已识别当前运行仓库，请核对路径。", "success")
            return
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
        self.dialog = VaultSourceDialog(payload, self.parent(), running=self.running)
        self.dialog.source_chosen.connect(self.accept)
        self.dialog.browse_requested.connect(self.browse)
        self.dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.dialog.open()
        self.set_status(f"发现 {len(payload.vaults)} 个仓库，请选择。" if payload.vaults else
                        (payload.issues[0].message if payload.issues else "未发现仓库，请浏览选择。"), "neutral")
