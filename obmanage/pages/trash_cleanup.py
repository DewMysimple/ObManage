from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSignalBlocker, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
)

from ..management import discover_vaults
from ..management.models import VaultCatalogResult
from ..management.trash import (
    TrashCleanupEngine,
    TrashOperation,
    TrashOperationResult,
    TrashPlan,
    TrashSafetyError,
    TrashVaultPreview,
)
from ..paths import canonical
from .common import FeaturePage, PathPicker, format_bytes, panel


_RECORD_STATUS_TEXT = {
    "backed_up": "已隔离，尚未清理",
    "success": "仓库已清理，可恢复",
    "partial": "部分清理，隔离备份保留",
    "failed": "清理失败，隔离备份保留",
    "cancelled": "清理取消，隔离备份保留",
    "restored": "已恢复，隔离备份待清理",
    "restore_partial": "恢复未完成，隔离备份保留",
    "finalizing": "永久清理中断，可尝试恢复或继续清理",
    "finalize_failed": "永久清理未开始，可尝试恢复或重试",
    "finalize_partial": "隔离备份部分清理",
    "finalized": "隔离备份已永久清理",
}

_RESTORABLE_STATUSES = frozenset({
    "backed_up", "success", "partial", "failed", "cancelled",
    "restore_partial", "finalizing", "finalize_failed", "finalize_partial",
})


def _path_key(path: str) -> str:
    return os.path.normcase(canonical(path)).casefold()


@dataclass(frozen=True)
class TrashScanResult:
    catalog: VaultCatalogResult
    plan: TrashPlan


@dataclass(frozen=True)
class TrashEntryRow:
    kind: str
    vault_name: str
    vault_root: str
    absolute_path: str
    size: int
    sha256: str


class TrashVaultTableModel(QAbstractTableModel):
    checked_changed = Signal()
    HEADERS = ("选择", "仓库", "文件", "目录", "大小", "完整位置")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[TrashVaultPreview] = []
        self._checked: set[str] = set()

    def set_rows(self, rows: Iterable[TrashVaultPreview]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self._checked.clear()
        self.endResetModel()
        self.checked_changed.emit()

    def checked_previews(self) -> tuple[TrashVaultPreview, ...]:
        return tuple(row for row in self.rows if _path_key(row.vault_root) in self._checked)

    def set_all(self, checked: bool) -> None:
        values = {
            _path_key(row.vault_root)
            for row in self.rows
            if row.file_count or row.dir_count
        } if checked else set()
        if values == self._checked:
            return
        self._checked = values
        if self.rows:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self.rows) - 1, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )
        self.checked_changed.emit()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        item = self.rows[index.row()]
        key = _path_key(item.vault_root)
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if key in self._checked else Qt.CheckState.Unchecked
        values: tuple[Any, ...] = (
            "",
            Path(item.vault_root).name or item.vault_root,
            item.file_count,
            item.dir_count,
            format_bytes(item.total_bytes),
            item.vault_root,
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            empty = "\n回收站为空，不需要清理。" if not item.entries else ""
            return f"仓库：{item.vault_root}\n仅处理：{item.trash_path}{empty}"
        if role == Qt.ItemDataRole.UserRole:
            if index.column() in (2, 3):
                return values[index.column()]
            if index.column() == 4:
                return item.total_bytes
            return str(values[index.column()]).casefold()
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (2, 3, 4):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if (index.isValid() and index.column() == 0
                and (self.rows[index.row()].file_count or self.rows[index.row()].dir_count)):
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value: Any,
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if (not index.isValid() or index.column() != 0
                or role != Qt.ItemDataRole.CheckStateRole):
            return False
        item = self.rows[index.row()]
        if not item.file_count and not item.dir_count:
            return False
        key = _path_key(item.vault_root)
        checked = Qt.CheckState(value) == Qt.CheckState.Checked
        if checked == (key in self._checked):
            return False
        if checked:
            self._checked.add(key)
        else:
            self._checked.discard(key)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.checked_changed.emit()
        return True


class TrashEntryTableModel(QAbstractTableModel):
    HEADERS = ("类型", "仓库", "回收站中的实际位置", "大小", "SHA-256")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[TrashEntryRow] = []

    def set_rows(self, rows: Iterable[TrashEntryRow]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        item = self.rows[index.row()]
        values = (
            "文件" if item.kind == "file" else "目录",
            item.vault_name,
            item.absolute_path,
            format_bytes(item.size),
            item.sha256[:12],
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"实际位置：{item.absolute_path}\n仓库：{item.vault_root}"
                f"\nSHA-256：{item.sha256}"
            )
        if role == Qt.ItemDataRole.UserRole:
            return item.size if index.column() == 3 else values[index.column()].casefold()
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 3:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None


class TrashCleanupPage(FeaturePage):
    """Preview, quarantine, restore and finally release vault-level trash."""

    page_key = "trash_cleanup"

    def __init__(self, state_dir: Path, settings: dict[str, Any], default_root: str) -> None:
        super().__init__(
            "回收站清理",
            "只处理真实仓库根目录下的 .trash；先完整隔离并校验，再清理所选内容。",
        )
        self.state_dir = Path(state_dir)
        self.plan: TrashPlan | None = None
        self.catalog: VaultCatalogResult | None = None
        self.current_operation: TrashOperation | None = None
        self._operations: tuple[TrashOperation, ...] = ()
        self._pending_operations: tuple[TrashOperation, ...] = ()
        self._recovery_blocked = False
        self._restoring = False

        _, config = panel(self.body)
        config_row = QHBoxLayout()
        self.root_picker = PathPicker(
            "仓库集合根目录",
            str(settings.get("root") or default_root),
            dialog_title="选择要检查回收站的仓库集合根目录",
        )
        self.root_picker.edit.setObjectName("trash_cleanup_root")
        config_row.addWidget(self.root_picker, 1)
        self.scan_button = QPushButton("扫描回收站")
        config_row.addWidget(self.scan_button)
        config.addLayout(config_row)

        _, vault_layout = panel(self.body)
        vault_header = QHBoxLayout()
        title = QLabel("仓库回收站")
        title.setObjectName("SectionTitle")
        vault_header.addWidget(title)
        self.vault_summary = QLabel("尚未扫描 · 默认不选择任何仓库")
        self.vault_summary.setObjectName("Muted")
        vault_header.addWidget(self.vault_summary, 1)
        self.select_all_button = QPushButton("全选非空项")
        self.select_all_button.setObjectName("TextButton")
        vault_header.addWidget(self.select_all_button)
        self.select_none_button = QPushButton("清空选择")
        self.select_none_button.setObjectName("TextButton")
        vault_header.addWidget(self.select_none_button)
        self.copy_vault_button = QPushButton("复制所选路径")
        self.copy_vault_button.setObjectName("TextButton")
        vault_header.addWidget(self.copy_vault_button)
        vault_layout.addLayout(vault_header)

        self.vault_table = QTableView()
        self.vault_table.setObjectName("trash_cleanup_vaults")
        self.vault_model = TrashVaultTableModel(self)
        self.vault_table.setModel(self.vault_model)
        self.vault_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.vault_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.vault_table.verticalHeader().hide()
        self.vault_table.setMinimumHeight(96)
        self.vault_table.setMaximumHeight(138)
        vault_header_view = self.vault_table.horizontalHeader()
        for column in (0, 1, 2, 3, 4):
            vault_header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        vault_header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        vault_layout.addWidget(self.vault_table)
        self.issues_label = QLabel("")
        self.issues_label.setObjectName("Muted")
        self.issues_label.setWordWrap(True)
        self.issues_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        vault_layout.addWidget(self.issues_label)

        _, entries_layout = panel(self.body)
        entry_header = QHBoxLayout()
        entry_title = QLabel("精确内容预览")
        entry_title.setObjectName("SectionTitle")
        entry_header.addWidget(entry_title)
        self.entry_summary = QLabel("等待扫描")
        self.entry_summary.setObjectName("Muted")
        entry_header.addWidget(self.entry_summary, 1)
        self.copy_entry_button = QPushButton("复制所选完整路径")
        self.copy_entry_button.setObjectName("TextButton")
        entry_header.addWidget(self.copy_entry_button)
        entries_layout.addLayout(entry_header)
        self.entry_table = QTableView()
        self.entry_table.setObjectName("trash_cleanup_entries")
        self.entry_model = TrashEntryTableModel(self)
        self.entry_table.setModel(self.entry_model)
        self.entry_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.entry_table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.entry_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.entry_table.setWordWrap(False)
        self.entry_table.verticalHeader().hide()
        entry_header_view = self.entry_table.horizontalHeader()
        for column in (0, 1, 3, 4):
            entry_header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        entry_header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        entries_layout.addWidget(self.entry_table, 1)
        self.body.setStretch(self.body.count() - 1, 1)

        _, recovery_layout = panel(self.body)
        recovery_header = QHBoxLayout()
        recovery_title = QLabel("持久隔离批次")
        recovery_title.setObjectName("SectionTitle")
        recovery_header.addWidget(recovery_title)
        self.operation_selector = QComboBox()
        self.operation_selector.setObjectName("trash_cleanup_operation_selector")
        self.operation_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.operation_selector.setMinimumContentsLength(18)
        self.operation_selector.setMaxVisibleItems(12)
        recovery_header.addWidget(self.operation_selector, 1)
        recovery_layout.addLayout(recovery_header)
        self.operation_status = QLabel("没有待处理隔离批次")
        self.operation_status.setObjectName("Muted")
        self.operation_status.setWordWrap(True)
        self.operation_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        recovery_layout.addWidget(self.operation_status)
        recovery_actions = QHBoxLayout()
        recovery_actions.addStretch()
        self.restore_button = QPushButton("恢复所选批次到空 .trash")
        recovery_actions.addWidget(self.restore_button)
        self.finalize_button = QPushButton("永久清理所选隔离备份")
        self.finalize_button.setObjectName("Danger")
        recovery_actions.addWidget(self.finalize_button)
        recovery_layout.addLayout(recovery_actions)
        self.finalize_confirm = QCheckBox("我确认永久清理所选批次（完成后不能恢复）")
        self.finalize_confirm.setObjectName("trash_cleanup_finalize_confirm")
        recovery_layout.addWidget(self.finalize_confirm)

        self.clear_confirm = QCheckBox("尚未扫描并选择回收站。")
        self.clear_confirm.setObjectName("trash_cleanup_confirm")
        self.clear_confirm.setEnabled(False)
        self.root_layout.addWidget(self.clear_confirm)
        actions = QHBoxLayout()
        self.safety_hint = QLabel(
            "仓库内容先复制到应用隔离区并逐文件校验；清理只删除预览中的项目并保留 .trash。"
        )
        self.safety_hint.setObjectName("Muted")
        self.safety_hint.setWordWrap(True)
        actions.addWidget(self.safety_hint, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Cancel")
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        self.clear_button = QPushButton("隔离并清理所选回收站")
        self.clear_button.setObjectName("Primary")
        actions.addWidget(self.clear_button)
        self.root_layout.addLayout(actions)

        self.root_picker.changed.connect(self._root_changed)
        self.scan_button.clicked.connect(self._start_scan)
        self.vault_model.checked_changed.connect(self._selection_changed)
        self.select_all_button.clicked.connect(lambda: self.vault_model.set_all(True))
        self.select_none_button.clicked.connect(lambda: self.vault_model.set_all(False))
        self.vault_table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_actions()
        )
        self.entry_table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_actions()
        )
        self.copy_vault_button.clicked.connect(self.copy_selected_vault_paths)
        self.copy_entry_button.clicked.connect(self.copy_selected_entry_paths)
        self.clear_confirm.toggled.connect(lambda *_: self.refresh_actions())
        self.finalize_confirm.toggled.connect(lambda *_: self.refresh_actions())
        self.clear_button.clicked.connect(self._start_clear)
        self.restore_button.clicked.connect(self._start_restore)
        self.finalize_button.clicked.connect(self._start_finalize)
        self.operation_selector.currentIndexChanged.connect(
            self._operation_selection_changed
        )
        self.cancel_button.clicked.connect(self.cancel_requested)
        self._reload_operations()
        self.refresh_actions()

    def settings_payload(self) -> dict[str, Any]:
        return {"root": self.root_picker.value}

    def repository_paths(self) -> tuple[str, ...]:
        return (self.root_picker.value,) if self.root_picker.value else ()

    @Slot()
    def _root_changed(self, *_: Any) -> None:
        if self._restoring:
            return
        self._invalidate_plan("范围已改变，请重新扫描回收站。")
        self.settings_changed.emit()

    def _invalidate_plan(self, message: str = "预览已失效，请重新扫描。") -> None:
        self.plan = None
        self.catalog = None
        self.vault_model.set_rows(())
        self.entry_model.set_rows(())
        self.vault_summary.setText("尚未扫描 · 默认不选择任何仓库")
        self.entry_summary.setText("等待扫描")
        self.issues_label.setText("")
        self.issues_label.setToolTip("")
        self._clear_confirmation()
        if not self._task_active:
            self.set_status(message)

    def _clear_confirmation(self) -> None:
        blocker = QSignalBlocker(self.clear_confirm)
        self.clear_confirm.setChecked(False)
        del blocker
        self.clear_confirm.setEnabled(False)
        self.clear_confirm.setText("尚未扫描并选择回收站。")

    def invalidate_confirmation(self) -> None:
        for checkbox in (self.clear_confirm, self.finalize_confirm):
            blocker = QSignalBlocker(checkbox)
            checkbox.setChecked(False)
            del blocker
        if self.plan is not None and self.vault_model.checked_previews():
            self.set_status("已离开本页面，本轮清理确认已撤销。", "warning")
        self.refresh_actions()

    def has_pending_recovery(self) -> bool:
        return self._recovery_blocked or bool(self._pending_operations)

    @Slot()
    def _selection_changed(self) -> None:
        selected = self.vault_model.checked_previews()
        blocker = QSignalBlocker(self.clear_confirm)
        self.clear_confirm.setChecked(False)
        del blocker
        files = sum(item.file_count for item in selected)
        directories = sum(item.dir_count for item in selected)
        size = sum(item.total_bytes for item in selected)
        if selected:
            self.clear_confirm.setText(
                f"我确认：仅清理所选 {len(selected)} 个仓库根级 .trash 中的 "
                f"{files} 个文件、{directories} 个目录（{format_bytes(size)}）；先保留隔离备份。"
            )
        else:
            self.clear_confirm.setText("请先明确勾选至少一个非空回收站。")
        self.vault_summary.setText(
            f"已扫描 {len(self.vault_model.rows)} 个回收站 · 已选 {len(selected)} 个"
        )
        self.refresh_actions()

    @Slot()
    def _start_scan(self) -> None:
        root = self.root_picker.value
        if not root:
            return
        self._invalidate_plan("正在扫描；此阶段只读取仓库。")

        def operation(cancel, progress):
            catalog = discover_vaults(root, cancel=cancel, progress=progress)
            plan = TrashCleanupEngine(self.state_dir).analyze(
                (vault.path for vault in catalog.vaults),
                cancel=cancel,
                progress=progress,
            )
            return TrashScanResult(catalog, plan)

        self.start_task("scan", operation)

    @Slot()
    def _start_clear(self) -> None:
        if self._external_recovery_pending or self.has_pending_recovery():
            self.set_status("另一个待恢复事务尚未处理，已阻止开始新的清理。", "warning")
            return
        selected = self.vault_model.checked_previews()
        if (self.plan is None or not selected or not self.clear_confirm.isChecked()
                or self._pending_operations or self._recovery_blocked):
            return
        plan = self.plan
        selected_paths = tuple(item.vault_root for item in selected)
        state_dir = self.state_dir
        self._clear_confirmation()

        def operation(cancel, progress):
            return TrashCleanupEngine(state_dir).execute(
                plan, selected_paths, cancel=cancel, progress=progress
            )

        self.start_task("clear", operation)

    @Slot()
    def _start_restore(self) -> None:
        if self.current_operation is None or not self._selected_operation_pending():
            return
        operation_id = self.current_operation.operation_id
        selected_paths = tuple(
            record.vault_root for record in self.current_operation.records
            if record.status in _RESTORABLE_STATUSES
        )
        if not selected_paths:
            return
        state_dir = self.state_dir
        self._reset_finalize_confirmation()

        def operation(cancel, progress):
            return TrashCleanupEngine(state_dir).restore(
                operation_id,
                selected_vaults=selected_paths,
                cancel=cancel,
                progress=progress,
            )

        self.start_task("restore", operation)

    @Slot()
    def _start_finalize(self) -> None:
        if (self.current_operation is None or not self._selected_operation_pending()
                or not self.finalize_confirm.isChecked()):
            return
        operation_id = self.current_operation.operation_id
        state_dir = self.state_dir
        self._reset_finalize_confirmation()

        def operation(cancel, _progress):
            return TrashCleanupEngine(state_dir).finalize(operation_id, cancel=cancel)

        self.start_task("finalize", operation)

    def _accept_scan(self, result: TrashScanResult) -> None:
        self.catalog = result.catalog
        self.plan = result.plan
        self.vault_model.set_rows(result.plan.vaults)
        rows: list[TrashEntryRow] = []
        for vault in result.plan.vaults:
            vault_name = Path(vault.vault_root).name or vault.vault_root
            for entry in vault.entries:
                rows.append(TrashEntryRow(
                    entry.kind,
                    vault_name,
                    vault.vault_root,
                    canonical(os.path.join(
                        vault.trash_path, *entry.relative_path.replace("\\", "/").split("/")
                    )),
                    entry.size,
                    entry.sha256,
                ))
        rows.sort(key=lambda item: (
            item.vault_root.casefold(), item.absolute_path.casefold(), item.absolute_path
        ))
        self.entry_model.set_rows(rows)
        files = sum(item.file_count for item in result.plan.vaults)
        directories = sum(item.dir_count for item in result.plan.vaults)
        size = sum(item.total_bytes for item in result.plan.vaults)
        issue_messages = [issue.message for issue in result.catalog.issues]
        issue_messages.extend(issue.message for issue in result.plan.issues)
        self.entry_summary.setText(
            f"{files} 个文件 · {directories} 个目录 · {format_bytes(size)}"
        )
        if issue_messages:
            self.issues_label.setText(
                f"扫描有 {len(issue_messages)} 个问题；首个：{issue_messages[0]}"
            )
            self.issues_label.setToolTip("\n".join(issue_messages))
        else:
            self.issues_label.setText("")
            self.issues_label.setToolTip("")
        self._selection_changed()
        if not result.plan.vaults:
            self.set_status("没有找到带根级 .trash 的有效仓库。", "warning")
        elif not rows:
            self.set_status("找到的仓库回收站均为空，不需要清理。", "success")
        elif issue_messages:
            self.set_status(
                f"已预览 {len(result.plan.vaults)} 个回收站；有 {len(issue_messages)} 个路径问题。",
                "warning",
            )
        else:
            self.set_status(
                f"扫描完成：{files} 个文件、{directories} 个目录；默认均未选择。",
                "success",
            )

    def _reset_finalize_confirmation(self) -> None:
        blocker = QSignalBlocker(self.finalize_confirm)
        self.finalize_confirm.setChecked(False)
        del blocker

    @staticmethod
    def _operation_pending(operation: TrashOperation) -> bool:
        return any(record.status != "finalized" for record in operation.records)

    def _selected_operation_pending(self) -> bool:
        if self.current_operation is None:
            return False
        current_id = self.current_operation.operation_id
        return any(
            operation.operation_id == current_id for operation in self._pending_operations
        )

    @staticmethod
    def _operation_choice_text(operation: TrashOperation) -> str:
        statuses = "/".join(sorted({
            _RECORD_STATUS_TEXT.get(record.status, record.status)
            for record in operation.records
        }))
        created = datetime.fromtimestamp(operation.created_at).strftime("%m-%d %H:%M")
        return f"{operation.operation_id[:8]} · {statuses} · {created}"

    @staticmethod
    def _operation_tooltip(operation: TrashOperation) -> str:
        created = datetime.fromtimestamp(operation.created_at).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"批次：{operation.operation_id}\n创建：{created}\n"
            + "\n".join(
                f"{record.vault_root}："
                f"{_RECORD_STATUS_TEXT.get(record.status, record.status)}"
                for record in operation.records
            )
        )

    def _show_current_operation(self) -> None:
        operation = self.current_operation
        if operation is None:
            self.operation_status.setText("没有待处理隔离批次")
            self.operation_status.setToolTip("")
            return
        statuses = sorted({_RECORD_STATUS_TEXT.get(item.status, item.status)
                           for item in operation.records})
        created = datetime.fromtimestamp(operation.created_at).strftime("%Y-%m-%d %H:%M:%S")
        prefix = "" if self._selected_operation_pending() else "最近已完成 · "
        self.operation_status.setText(
            f"{prefix}{' / '.join(statuses)} · {operation.operation_id[:8]} · "
            f"{len(operation.records)} 个仓库 · {created}"
        )
        self.operation_status.setToolTip(self._operation_tooltip(operation))

    @Slot(int)
    def _operation_selection_changed(self, index: int) -> None:
        self._reset_finalize_confirmation()
        operation_id = self.operation_selector.itemData(index) if index >= 0 else None
        self.current_operation = next(
            (operation for operation in self._operations
             if operation.operation_id == operation_id),
            None,
        )
        self._show_current_operation()
        self.refresh_actions()

    def _reload_operations(self) -> None:
        selected_id = (
            self.current_operation.operation_id if self.current_operation is not None else None
        )
        self._reset_finalize_confirmation()
        try:
            operations = TrashCleanupEngine(self.state_dir).list_operations()
        except (OSError, TrashSafetyError) as exc:
            self._operations = ()
            self._pending_operations = ()
            self.current_operation = None
            self._recovery_blocked = True
            blocker = QSignalBlocker(self.operation_selector)
            self.operation_selector.clear()
            del blocker
            self.operation_status.setText(f"隔离记录无法安全读取：{exc}")
            self.operation_status.setToolTip(str(exc))
            return
        self._recovery_blocked = False
        self._operations = operations
        self._pending_operations = tuple(
            operation for operation in operations
            if self._operation_pending(operation)
        )
        choices = self._pending_operations or operations[:1]
        blocker = QSignalBlocker(self.operation_selector)
        self.operation_selector.clear()
        for operation in choices:
            self.operation_selector.addItem(
                self._operation_choice_text(operation), operation.operation_id
            )
            index = self.operation_selector.count() - 1
            self.operation_selector.setItemData(
                index, self._operation_tooltip(operation), Qt.ItemDataRole.ToolTipRole
            )
        wanted_index = self.operation_selector.findData(selected_id) if selected_id else -1
        self.operation_selector.setCurrentIndex(wanted_index if wanted_index >= 0 else 0)
        del blocker
        if not operations:
            self.current_operation = None
            self._show_current_operation()
            return
        selected = self.operation_selector.currentData()
        self.current_operation = next(
            (operation for operation in operations if operation.operation_id == selected),
            choices[0],
        )
        self._show_current_operation()

    def _accept_operation_result(self, kind: str, result: TrashOperationResult) -> None:
        if kind == "clear":
            self._invalidate_plan("清理操作结束；再次清理前必须重新扫描。")
        self._reload_operations()
        failure = result.failures[0].message if result.failures else ""
        if result.status == "success":
            if kind == "clear":
                removed_files = sum(item.removed_files for item in result.vaults)
                removed_dirs = sum(item.removed_dirs for item in result.vaults)
                self.set_status(
                    f"已从仓库清理 {removed_files} 个文件、{removed_dirs} 个目录；"
                    "隔离备份仍保留，可恢复或永久清理。",
                    "success",
                )
            elif kind == "restore":
                self.set_status("隔离内容已恢复；备份仍保留，可确认永久清理。", "success")
            else:
                self.set_status(
                    f"隔离备份已永久清理，实际释放 {format_bytes(result.bytes_freed)}。",
                    "success",
                )
        elif result.status == "cancelled":
            self.set_status("操作已取消；已建立的隔离备份仍会显示在本页。", "warning")
        elif result.status == "rejected":
            self.set_status(f"操作被安全拒绝：{failure or '范围或内容已变化。'}", "error")
        else:
            self.set_status(f"操作未完整完成：{failure or result.status}", "error")
        self.message_logged.emit(self.status_label.text())

    def task_finished(self, status: str, payload: Any) -> None:
        kind = self._task_kind
        super().task_finished(status, payload)
        if status != "ok":
            if kind == "scan":
                self._invalidate_plan(
                    "扫描已取消，请重新开始。" if status == "cancelled" else str(payload)
                )
            self._task_kind = ""
            self.refresh_actions()
            return
        if kind == "scan":
            self._accept_scan(payload)
        elif kind in {"clear", "restore", "finalize"}:
            self._accept_operation_result(kind, payload)
        self._task_kind = ""
        self.refresh_actions()

    def refresh_actions(self) -> None:
        if not hasattr(self, "root_picker"):
            return
        available = not self._global_busy and not self._task_active
        pending = bool(self._pending_operations)
        selected_pending = self._selected_operation_pending()
        self.root_picker.set_controls_enabled(available)
        self.scan_button.setEnabled(available and bool(self.root_picker.value))
        self.vault_table.setEnabled(available)
        self.select_all_button.setEnabled(available and bool(self.vault_model.rows))
        self.select_none_button.setEnabled(available and bool(self.vault_model.checked_previews()))
        selected = bool(self.vault_model.checked_previews())
        self.clear_confirm.setEnabled(
            available and not pending and not self._external_recovery_pending
            and not self._recovery_blocked
            and self.plan is not None and selected
        )
        self.clear_button.setEnabled(
            available and not pending and not self._external_recovery_pending
            and not self._recovery_blocked
            and self.plan is not None and selected and self.clear_confirm.isChecked()
        )
        self.operation_selector.setEnabled(
            available and not self._recovery_blocked and self.operation_selector.count() > 0
        )
        self.finalize_confirm.setEnabled(
            available and selected_pending and not self._recovery_blocked
        )
        statuses = (
            {record.status for record in self.current_operation.records}
            if self.current_operation is not None else set()
        )
        restorable = bool(statuses & _RESTORABLE_STATUSES)
        self.restore_button.setEnabled(
            available and selected_pending and not self._recovery_blocked and restorable
        )
        self.finalize_button.setEnabled(
            available and selected_pending and not self._recovery_blocked
            and self.finalize_confirm.isChecked()
        )
        cancellable = self._task_active and self._task_kind in {
            "scan", "clear", "restore", "finalize"
        }
        self.cancel_button.setVisible(cancellable)
        self.cancel_button.setEnabled(cancellable)
        self.copy_vault_button.setEnabled(
            bool(self.vault_table.selectionModel().selectedRows())
        )
        self.copy_entry_button.setEnabled(
            bool(self.entry_table.selectionModel().selectedRows())
        )

    def copy_selected_vault_paths(self) -> None:
        paths = [
            self.vault_model.rows[index.row()].vault_root
            for index in self.vault_table.selectionModel().selectedRows()
        ]
        if paths:
            QApplication.clipboard().setText("\n".join(paths))

    def copy_selected_entry_paths(self) -> None:
        paths = [
            self.entry_model.rows[index.row()].absolute_path
            for index in self.entry_table.selectionModel().selectedRows()
        ]
        if paths:
            QApplication.clipboard().setText("\n".join(paths))


__all__ = [
    "TrashCleanupPage",
    "TrashEntryRow",
    "TrashEntryTableModel",
    "TrashScanResult",
    "TrashVaultTableModel",
]
