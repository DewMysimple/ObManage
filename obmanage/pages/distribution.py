from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSignalBlocker,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from ..management import discover_vaults, read_opened_vault_candidates
from ..management.deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentError,
    DeploymentPlan,
    DeploymentRequest,
    DeploymentResult,
    DeploymentSelection,
    DeploymentTarget,
)
from ..management.journal import JournalBatch, JournalError
from ..management.models import (
    ManagementIssue,
    OpenedVaultCandidates,
    VaultCatalogResult,
    VaultInfo,
)
from ..models import Progress, SyncCancelled
from ..paths import canonical, native
from .common import FeaturePage, PathPicker, format_bytes, panel


_ACTION_TEXT = {
    "add": "新增",
    "update": "更新",
    "delete": "删除",
    "skip": "跳过",
}

_ACTION_COLORS = {
    "add": "#27735B",
    "update": "#326B94",
    "delete": "#AD5748",
    "skip": "#79848B",
}

_FINALIZE_STATUSES = frozenset({"committed", "finalizing", "finalize_required"})
_ROLLBACK_STATUSES = frozenset({
    "preparing",
    "prepared",
    "committing",
    "commit_failed",
    "committed",
    "failed",
    "recovery_required",
    "rollback_required",
    "rollback_blocked",
    "rolling_back",
    "finalizing",
    "finalize_required",
})
_PENDING_STATUSES = _FINALIZE_STATUSES | _ROLLBACK_STATUSES
_KNOWN_BATCH_LABELS = frozenset({
    "obmanage-ui:template_suite",
    "obmanage-ui:obsidian_config",
    "obmanage-ui:templater",
})
_LEGACY_RECOVERY_PAGE = "obsidian_config"

_STATUS_TEXT = {
    "preparing": "准备中断，建议撤销恢复",
    "prepared": "已准备，尚待恢复",
    "committing": "提交中断，必须检查并撤销",
    "commit_failed": "提交失败，必须撤销",
    "committed": "已部署，等待确认保留或撤销",
    "failed": "部署失败，等待恢复",
    "prepare_failed": "准备失败，目标未提交",
    "cancelled": "已取消，目标未提交",
    "recovery_required": "自动恢复未完成，需要继续撤销",
    "rolling_back": "撤销中断，需要继续撤销",
    "rollback_required": "撤销未完成，需要继续撤销",
    "rollback_blocked": "目标已变化，撤销被安全阻止",
    "rolled_back": "已撤销",
    "rolled_back_with_residuals": "已撤销，保留未授权的残留目录",
    "finalizing": "确认清理中断，可继续确认或撤销",
    "finalize_required": "备份清理未完成，可重试确认或撤销",
    "finalized": "已确认保留",
}


def _physical_key(path: str) -> str:
    value = canonical(path)
    try:
        value = canonical(os.path.realpath(native(value)))
    except OSError:
        pass
    return os.path.normcase(value).casefold()


def _paths_overlap(first: str, second: str) -> bool:
    if not first or not second:
        return False
    first_key = _physical_key(first)
    second_key = _physical_key(second)
    try:
        common = os.path.commonpath((first_key, second_key))
    except ValueError:
        return False
    return common in (first_key, second_key)


def _target_id(path: str) -> str:
    digest = hashlib.sha256(_physical_key(path).encode("utf-8")).hexdigest()
    return f"vault-{digest[:20]}"


def _absolute_change_path(target_root: str, relative_path: str) -> str:
    parts = relative_path.replace("\\", "/").split("/")
    return canonical(os.path.join(target_root, *parts))


@dataclass(frozen=True)
class ComponentOption:
    component_id: str
    title: str
    relative_path: str
    default_checked: bool = False


@dataclass(frozen=True)
class DeploymentPreviewRow:
    action: str
    component_id: str
    target_name: str
    target_root: str
    absolute_path: str
    size: int
    reason: str
    kind: str = "file"


@dataclass(frozen=True)
class OpenVaultState:
    """One read of Obsidian's open-vault registry and optional catalog validation."""

    paths: tuple[str, ...] = ()
    vaults: tuple[VaultInfo, ...] = ()
    issues: tuple[ManagementIssue, ...] = ()
    reliable: bool = True


@dataclass(frozen=True)
class CatalogTaskResult:
    catalog: VaultCatalogResult
    open_state: OpenVaultState


@dataclass(frozen=True)
class AnalyzeTaskResult:
    plan: DeploymentPlan
    open_state: OpenVaultState


@dataclass(frozen=True)
class ExecuteTaskResult:
    result: DeploymentResult
    open_state: OpenVaultState


class VaultTargetTableModel(QAbstractTableModel):
    """Catalog-backed targets; rows are deliberately unchecked after every scan."""

    checked_changed = Signal()
    HEADERS = ("选择", "仓库", "完整位置")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[VaultInfo] = []
        self._checked: set[str] = set()

    def set_rows(self, rows: Iterable[VaultInfo]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self._checked.clear()
        self.endResetModel()
        self.checked_changed.emit()

    def checked_vaults(self) -> tuple[VaultInfo, ...]:
        return tuple(item for item in self.rows if _physical_key(item.path) in self._checked)

    def set_all_checked(self, checked: bool) -> None:
        wanted = {_physical_key(item.path) for item in self.rows} if checked else set()
        if wanted == self._checked:
            return
        self._checked = wanted
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
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return (Qt.CheckState.Checked if _physical_key(item.path) in self._checked
                    else Qt.CheckState.Unchecked)
        if role == Qt.ItemDataRole.DisplayRole:
            return ("", item.name, item.path)[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            nested = f"\n嵌套于：{item.parent_path}" if item.parent_path else ""
            return f"{item.path}{nested}"
        if role == Qt.ItemDataRole.UserRole:
            return ("", item.name.casefold(), item.path.casefold())[index.column()]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value: Any,
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if (not index.isValid() or index.column() != 0
                or role != Qt.ItemDataRole.CheckStateRole):
            return False
        key = _physical_key(self.rows[index.row()].path)
        checked = Qt.CheckState(value) == Qt.CheckState.Checked
        changed = checked != (key in self._checked)
        if not changed:
            return False
        if checked:
            self._checked.add(key)
        else:
            self._checked.discard(key)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.checked_changed.emit()
        return True


class DeploymentPreviewTableModel(QAbstractTableModel):
    HEADERS = ("操作", "组件", "目标仓库", "实际修改位置", "大小")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[DeploymentPreviewRow] = []

    def set_rows(self, rows: Iterable[DeploymentPreviewRow]) -> None:
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
        action_text = _ACTION_TEXT.get(item.action, item.action)
        if item.kind in {"dir", "directory", "folder"}:
            action_text += "文件夹"
        values = (
            action_text,
            item.component_id,
            item.target_name,
            item.absolute_path,
            format_bytes(item.size),
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            reason = f"\n{item.reason}" if item.reason else ""
            kind = "文件夹" if item.kind in {"dir", "directory", "folder"} else "文件"
            return (f"实际位置：{item.absolute_path}\n目标仓库：{item.target_root}"
                    f"\n类型：{kind}{reason}")
        if role == Qt.ItemDataRole.UserRole:
            return (
                ("add", "update", "delete", "skip").index(item.action)
                if index.column() == 0 and item.action in _ACTION_TEXT
                else values[index.column()].casefold()
                if isinstance(values[index.column()], str)
                else item.size
            )
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 0:
            from PySide6.QtGui import QColor
            return QColor(_ACTION_COLORS.get(item.action, "#23353F"))
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 4:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None


class DistributionPage(FeaturePage):
    """Shared Qt workflow for bounded, exact component deployments."""

    page_key = "distribution"
    component_options: tuple[ComponentOption, ...] = ()
    source_label = "模板来源"
    source_hint = "选择包含待部署组件的目录"
    supports_opened_sources = False

    def __init__(self, state_dir: Path, settings: dict[str, Any], default_root: str,
                 *, title: str, description: str,
                 obsidian_config_path: str | Path | None = None,
                 opened_vault_reader: Callable[[str | Path | None], OpenedVaultCandidates]
                 | None = None) -> None:
        super().__init__(title, description)
        self.state_dir = Path(state_dir)
        self.default_root = default_root
        self.obsidian_config_path = obsidian_config_path
        self.opened_vault_reader = opened_vault_reader or read_opened_vault_candidates
        self.plan: DeploymentPlan | None = None
        self.catalog: VaultCatalogResult | None = None
        self.current_batch: JournalBatch | None = None
        self._batches: tuple[JournalBatch, ...] = ()
        self._pending_batches: tuple[JournalBatch, ...] = ()
        self._recovery_blocked = False
        self.open_state = OpenVaultState(
            issues=(ManagementIssue(
                "occupancy_not_checked",
                "尚未检查 Obsidian 当前打开的仓库。",
                severity="warning",
            ),),
            reliable=False,
        )
        self._restoring = False

        _, config = panel(self.body)
        self.root_picker = PathPicker(
            "仓库集合根目录", "", dialog_title="选择要发现目标仓库的集合根目录"
        )
        self.root_picker.edit.setObjectName(f"{self.page_key}_root")
        config.addWidget(self.root_picker)
        self.source_picker = PathPicker(
            self.source_label, "", dialog_title=self.source_hint
        )
        self.source_picker.edit.setObjectName(f"{self.page_key}_source")
        self.source_picker.edit.setPlaceholderText(self.source_hint)
        config.addWidget(self.source_picker)
        self.opened_source_button: QPushButton | None = None
        self.opened_source_menu: QMenu | None = None
        self.opened_source_status: QLabel | None = None
        if self.supports_opened_sources:
            opened_row = QHBoxLayout()
            opened_row.addSpacing(116)
            self.opened_source_button = QPushButton("从 Obsidian 当前打开仓库选择…")
            self.opened_source_button.setObjectName(f"{self.page_key}_opened_source")
            self.opened_source_button.setToolTip(
                "仅在点击后读取 Obsidian 的本机配置；候选仍会经过真实仓库校验。"
            )
            opened_row.addWidget(self.opened_source_button)
            self.opened_source_status = QLabel("不会自动读取 Obsidian 配置")
            self.opened_source_status.setObjectName("Muted")
            self.opened_source_status.setWordWrap(True)
            opened_row.addWidget(self.opened_source_status, 1)
            config.addLayout(opened_row)
            self.opened_source_menu = QMenu(self)

        _, component_layout = panel(self.body)
        component_grid = QGridLayout()
        component_grid.setHorizontalSpacing(12)
        component_grid.setVerticalSpacing(5)
        component_caption = QLabel("部署组件")
        component_caption.setFixedWidth(106)
        component_grid.addWidget(component_caption, 0, 0, 2, 1)
        self.component_boxes: dict[str, QCheckBox] = {}
        for index, option in enumerate(self.component_options):
            box = QCheckBox(option.title)
            box.setObjectName(f"{self.page_key}_component_{option.component_id}")
            box.setToolTip(
                f"来源范围：{option.relative_path}\n目标范围：仓库根目录/{option.relative_path}"
            )
            self.component_boxes[option.component_id] = box
            component_grid.addWidget(box, index // 2, index % 2 + 1)
        component_grid.setColumnStretch(3, 1)
        component_layout.addLayout(component_grid)
        self.component_scope = QLabel("每个组件都按完整克隆处理：目标独有内容会进入删除预览。")
        self.component_scope.setObjectName("Muted")
        self.component_scope.setWordWrap(True)
        component_layout.addWidget(self.component_scope)

        target_frame, target_layout = panel(self.body)
        target_header = QHBoxLayout()
        target_title = QLabel("目标仓库")
        target_title.setObjectName("SectionTitle")
        target_header.addWidget(target_title)
        self.target_summary = QLabel("尚未发现仓库 · 默认不选择任何目标")
        self.target_summary.setObjectName("Muted")
        target_header.addWidget(self.target_summary, 1)
        target_layout.addLayout(target_header)
        target_controls = QHBoxLayout()
        target_controls.addStretch()
        self.copy_target_button = QPushButton("复制所选路径")
        self.copy_target_button.setObjectName("TextButton")
        target_controls.addWidget(self.copy_target_button)
        self.select_all_targets_button = QPushButton("全选目标")
        self.select_all_targets_button.setObjectName("TextButton")
        target_controls.addWidget(self.select_all_targets_button)
        self.clear_targets_button = QPushButton("清空选择")
        self.clear_targets_button.setObjectName("TextButton")
        target_controls.addWidget(self.clear_targets_button)
        self.catalog_button = QPushButton("发现仓库")
        target_controls.addWidget(self.catalog_button)
        target_layout.addLayout(target_controls)
        self.target_table = QTableView()
        self.target_table.setObjectName(f"{self.page_key}_targets")
        self.target_model = VaultTargetTableModel(self)
        self.target_table.setModel(self.target_model)
        self.target_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.target_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.target_table.verticalHeader().hide()
        self.target_table.setMinimumHeight(100)
        self.target_table.setMaximumHeight(150)
        target_header_view = self.target_table.horizontalHeader()
        target_header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        target_header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        target_header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        target_layout.addWidget(self.target_table)
        self.catalog_issues = QLabel("")
        self.catalog_issues.setObjectName("Muted")
        self.catalog_issues.setWordWrap(True)
        self.catalog_issues.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        target_layout.addWidget(self.catalog_issues)

        self.occupancy_status = QLabel("占用状态：尚未检查")
        self.occupancy_status.setObjectName("Muted")
        self.occupancy_status.setWordWrap(True)
        self.occupancy_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        target_layout.addWidget(self.occupancy_status)
        self.occupancy_confirm = QCheckBox(
            "无法自动确认占用；我已关闭所有所选目标仓库"
        )
        self.occupancy_confirm.setObjectName(f"{self.page_key}_occupancy_confirm")
        self.occupancy_confirm.hide()
        target_layout.addWidget(self.occupancy_confirm)

        preview_frame, preview_layout = panel(self.body)
        preview_header = QHBoxLayout()
        preview_title = QLabel("文件与目录预览")
        preview_title.setObjectName("SectionTitle")
        preview_header.addWidget(preview_title)
        self.preview_summary = QLabel("新增 — · 更新 — · 删除：尚未分析 · 跳过 —")
        self.preview_summary.setObjectName("Muted")
        preview_header.addWidget(self.preview_summary, 1)
        self.copy_preview_button = QPushButton("复制所选完整路径")
        self.copy_preview_button.setObjectName("TextButton")
        preview_header.addWidget(self.copy_preview_button)
        preview_layout.addLayout(preview_header)
        self.preview_table = QTableView()
        self.preview_table.setObjectName(f"{self.page_key}_preview")
        self.preview_model = DeploymentPreviewTableModel(self)
        self.preview_table.setModel(self.preview_model)
        self.preview_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.preview_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.preview_table.setWordWrap(False)
        self.preview_table.verticalHeader().hide()
        preview_header_view = self.preview_table.horizontalHeader()
        for column in (0, 1, 2, 4):
            preview_header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        preview_header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        preview_layout.addWidget(self.preview_table, 1)
        self.body.setStretch(self.body.count() - 1, 1)

        recovery_frame, recovery_layout = panel(self.body)
        recovery_frame.setObjectName("Panel")
        recovery_row = QHBoxLayout()
        recovery_title = QLabel("持久部署批次")
        recovery_title.setObjectName("SectionTitle")
        recovery_row.addWidget(recovery_title)
        self.batch_selector = QComboBox()
        self.batch_selector.setObjectName(f"{self.page_key}_batch_selector")
        self.batch_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.batch_selector.setMinimumContentsLength(18)
        self.batch_selector.setMaxVisibleItems(12)
        recovery_row.addWidget(self.batch_selector, 1)
        recovery_layout.addLayout(recovery_row)
        self.batch_status = QLabel("没有待处理批次")
        self.batch_status.setObjectName("Muted")
        self.batch_status.setWordWrap(True)
        self.batch_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        recovery_layout.addWidget(self.batch_status)
        recovery_actions = QHBoxLayout()
        recovery_actions.addStretch()
        self.rollback_button = QPushButton("撤销所选批次")
        self.rollback_button.setObjectName("Danger")
        recovery_actions.addWidget(self.rollback_button)
        self.finalize_button = QPushButton("保留所选批次并清理备份")
        recovery_actions.addWidget(self.finalize_button)
        recovery_layout.addLayout(recovery_actions)
        self.finalize_confirm = QCheckBox(
            "我确认永久清理所选批次备份（完成后不能撤销）"
        )
        self.finalize_confirm.setObjectName(f"{self.page_key}_finalize_confirm")
        recovery_layout.addWidget(self.finalize_confirm)

        self.confirm_checkbox = QCheckBox(
            "尚未生成计划；分析完成后必须确认目标与删除数量。"
        )
        self.confirm_checkbox.setObjectName(f"{self.page_key}_confirm")
        self.confirm_checkbox.setEnabled(False)
        self.root_layout.addWidget(self.confirm_checkbox)
        actions = QHBoxLayout()
        self.safety_hint = QLabel("分析只读取来源和目标；执行只修改已勾选仓库中的选定子树。")
        self.safety_hint.setObjectName("Muted")
        self.safety_hint.setWordWrap(True)
        actions.addWidget(self.safety_hint, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Cancel")
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        self.analyze_button = QPushButton("分析文件与目录差异")
        actions.addWidget(self.analyze_button)
        self.execute_button = QPushButton("开始部署")
        self.execute_button.setObjectName("Primary")
        actions.addWidget(self.execute_button)
        self.root_layout.addLayout(actions)

        self.restore_settings(settings)
        self.root_picker.changed.connect(lambda *_: self._inputs_changed(clear_catalog=True))
        self.source_picker.changed.connect(lambda *_: self._inputs_changed(clear_catalog=True))
        for box in self.component_boxes.values():
            box.toggled.connect(lambda *_: self._inputs_changed(clear_catalog=False))
        self.target_model.checked_changed.connect(self._targets_changed)
        self.target_table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_actions()
        )
        self.preview_table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_actions()
        )
        self.catalog_button.clicked.connect(self._start_catalog)
        if self.opened_source_button is not None:
            self.opened_source_button.clicked.connect(self._start_opened_sources)
        self.analyze_button.clicked.connect(self._start_analyze)
        self.execute_button.clicked.connect(self._start_execute)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.rollback_button.clicked.connect(self._start_rollback)
        self.finalize_button.clicked.connect(self._start_finalize)
        self.batch_selector.currentIndexChanged.connect(self._batch_selection_changed)
        self.finalize_confirm.toggled.connect(lambda *_: self.refresh_actions())
        self.confirm_checkbox.toggled.connect(lambda *_: self.refresh_actions())
        self.occupancy_confirm.toggled.connect(lambda *_: self.refresh_actions())
        self.copy_target_button.clicked.connect(self.copy_selected_target_paths)
        self.select_all_targets_button.clicked.connect(
            lambda: self.target_model.set_all_checked(True)
        )
        self.clear_targets_button.clicked.connect(
            lambda: self.target_model.set_all_checked(False)
        )
        self.copy_preview_button.clicked.connect(self.copy_selected_preview_paths)
        self._reload_persistent_batches()
        self.refresh_actions()

    @property
    def batch_label(self) -> str:
        return f"obmanage-ui:{self.page_key}"

    def settings_payload(self) -> dict[str, Any]:
        return {
            "root": self.root_picker.value,
            "source": self.source_picker.value,
            "components": [
                key for key, box in self.component_boxes.items() if box.isChecked()
            ],
        }

    def repository_paths(self) -> tuple[str, ...]:
        """Expose configured repository/source roots for global state checks."""
        return tuple(dict.fromkeys(
            value for value in (self.root_picker.value, self.source_picker.value)
            if value
        ))

    def restore_settings(self, settings: dict[str, Any]) -> None:
        """Restore durable inputs; target choices intentionally never persist."""
        self._restoring = True
        controls = [self.root_picker.edit, self.source_picker.edit, *self.component_boxes.values()]
        blockers = [QSignalBlocker(control) for control in controls]
        try:
            self.root_picker.set_value(str(settings.get("root") or self.default_root))
            self.source_picker.set_value(str(settings.get("source") or ""))
            raw_components = settings.get("components")
            selected = ({str(value) for value in raw_components}
                        if isinstance(raw_components, (list, tuple))
                        else {item.component_id for item in self.component_options
                              if item.default_checked})
            for key, box in self.component_boxes.items():
                box.setChecked(key in selected)
        finally:
            del blockers
            self._restoring = False
        if hasattr(self, "target_model"):
            self.target_model.set_rows(())
        self.catalog = None
        self._invalidate_plan("设置已恢复，请重新发现目标并分析。")

    def selected_component_ids(self) -> tuple[str, ...]:
        return tuple(key for key, box in self.component_boxes.items() if box.isChecked())

    def _components(self) -> tuple[DeploymentComponent, ...]:
        raise NotImplementedError

    def _build_request(self) -> DeploymentRequest:
        components = self._components()
        targets = self.target_model.checked_vaults()
        selections = tuple(
            DeploymentSelection(
                component,
                DeploymentTarget(_target_id(vault.path), vault.path),
            )
            for component in components
            for vault in targets
        )
        return DeploymentRequest(selections, label=self.batch_label)

    @Slot()
    def _inputs_changed(self, *_: Any, clear_catalog: bool = False) -> None:
        if self._restoring:
            return
        if clear_catalog:
            self.catalog = None
            self.target_model.set_rows(())
            self.target_summary.setText("输入已改变 · 请重新发现仓库 · 默认不选择任何目标")
            self.catalog_issues.setText("")
            self.catalog_issues.setToolTip("")
        self._invalidate_plan("输入已改变，请重新分析文件与目录差异。")
        self._update_occupancy_display(reset_confirmation=True)
        self.settings_changed.emit()
        self.refresh_actions()

    @Slot()
    def _targets_changed(self) -> None:
        if self._restoring:
            return
        self._invalidate_plan("目标选择已改变，请重新分析文件与目录差异。")
        selected = len(self.target_model.checked_vaults())
        self.target_summary.setText(
            f"已发现 {len(self.target_model.rows):,} 个真实仓库 · 已选 {selected:,} 个"
        )
        self._update_occupancy_display(reset_confirmation=True)
        self.refresh_actions()

    def _invalidate_plan(self, message: str = "计划已失效，请重新分析。") -> None:
        self.plan = None
        self.preview_model.set_rows(())
        self.preview_summary.setText("新增 — · 更新 — · 删除：尚未分析 · 跳过 —")
        blocker = QSignalBlocker(self.confirm_checkbox)
        self.confirm_checkbox.setChecked(False)
        del blocker
        self.confirm_checkbox.setText("尚未生成计划；分析完成后必须确认目标与删除数量。")
        self.confirm_checkbox.setEnabled(False)
        if not self._task_active:
            self.set_status(message)

    def invalidate_confirmation(self) -> None:
        for checkbox in (self.confirm_checkbox, self.finalize_confirm):
            blocker = QSignalBlocker(checkbox)
            checkbox.setChecked(False)
            del blocker
        if self.plan is not None and self.plan.needs_deploy:
            self.set_status("已离开本页面，本轮部署确认已撤销。", "warning")
        self.refresh_actions()

    def has_pending_recovery(self) -> bool:
        return self._recovery_blocked or bool(self._pending_batches)

    def task_cancellable(self) -> bool:
        return self._task_active and self._task_kind in {
            "opened_sources", "catalog", "analyze", "execute"
        }

    def _source_overlaps_vault(self, vault: VaultInfo) -> bool:
        return _paths_overlap(self.source_picker.value, vault.path)

    def _read_open_state(self, cancel, progress, *, validate: bool) -> OpenVaultState:
        if cancel is not None and cancel.is_set():
            raise SyncCancelled("已取消读取 Obsidian 当前打开仓库。")
        try:
            opened = self.opened_vault_reader(self.obsidian_config_path)
        except Exception as exc:
            issue = ManagementIssue(
                "opened_vault_check_failed",
                f"无法读取 Obsidian 当前打开仓库：{type(exc).__name__}: {exc}",
            )
            return OpenVaultState(issues=(issue,), reliable=False)
        if not isinstance(opened, OpenedVaultCandidates):
            issue = ManagementIssue(
                "opened_vault_check_invalid",
                "Obsidian 当前打开仓库读取器返回了无效结果。",
            )
            return OpenVaultState(issues=(issue,), reliable=False)
        paths_by_key = {_physical_key(path): path for path in opened.paths}
        paths = tuple(sorted(paths_by_key.values(), key=lambda item: (item.casefold(), item)))
        issues = list(opened.issues)
        vaults: tuple[VaultInfo, ...] = ()
        if validate and paths:
            validated = discover_vaults(paths, cancel=cancel, progress=progress)
            vaults = tuple(
                vault for vault in validated.vaults
                if _physical_key(vault.path) in paths_by_key
            )
            issues.extend(validated.issues)
            if len(vaults) != len(paths):
                issues.append(ManagementIssue(
                    "opened_vault_not_valid",
                    "部分 Obsidian 当前打开仓库未通过真实仓库校验。",
                    severity="warning",
                ))
        if progress is not None:
            progress(Progress(
                phase="occupancy",
                message="已读取 Obsidian 当前打开仓库状态",
                completed_files=len(paths),
                total_files=len(paths),
            ))
        if cancel is not None and cancel.is_set():
            raise SyncCancelled("已取消读取 Obsidian 当前打开仓库。")
        return OpenVaultState(
            paths=paths,
            vaults=vaults,
            issues=tuple(issues),
            reliable=not issues,
        )

    @staticmethod
    def _open_selected_targets(state: OpenVaultState,
                               target_paths: Iterable[str]) -> tuple[str, ...]:
        open_keys = {_physical_key(path) for path in state.paths}
        return tuple(path for path in target_paths if _physical_key(path) in open_keys)

    def _selected_target_paths(self) -> tuple[str, ...]:
        return tuple(vault.path for vault in self.target_model.checked_vaults())

    def _update_occupancy_display(self, *, reset_confirmation: bool = True,
                                  target_paths: Iterable[str] | None = None) -> None:
        paths = tuple(target_paths) if target_paths is not None else self._selected_target_paths()
        open_targets = self._open_selected_targets(self.open_state, paths)
        if reset_confirmation:
            blocker = QSignalBlocker(self.occupancy_confirm)
            self.occupancy_confirm.setChecked(False)
            del blocker
        if not self.open_state.reliable:
            first = self.open_state.issues[0].message if self.open_state.issues else "原因未知"
            self.occupancy_status.setText(f"占用状态无法自动确认：{first}")
            self.occupancy_status.setToolTip("\n".join(
                f"{issue.message}{' · ' + issue.path if issue.path else ''}"
                for issue in self.open_state.issues
            ))
            self.occupancy_confirm.setVisible(bool(paths))
        elif open_targets:
            self.occupancy_status.setText(
                f"已阻止：{len(open_targets)} 个所选目标正被 Obsidian 标记为打开。"
            )
            self.occupancy_status.setToolTip("\n".join(open_targets))
            self.occupancy_confirm.hide()
        else:
            self.occupancy_status.setText(
                f"占用检查通过：当前记录 {len(self.open_state.paths)} 个打开仓库，"
                "所选目标均未标记为打开。"
            )
            self.occupancy_status.setToolTip("")
            self.occupancy_confirm.hide()

    @Slot()
    def _start_opened_sources(self) -> None:
        if self.opened_source_button is None:
            return

        def operation(cancel, progress):
            return self._read_open_state(cancel, progress, validate=True)

        self.start_task("opened_sources", operation)

    def _accept_opened_sources(self, state: OpenVaultState) -> None:
        self.open_state = state
        self._update_occupancy_display()
        assert self.opened_source_menu is not None
        self.opened_source_menu.clear()
        for vault in state.vaults:
            action = self.opened_source_menu.addAction(f"{vault.name}  ·  {vault.path}")
            action.setToolTip(vault.path)
            action.triggered.connect(
                lambda checked=False, path=vault.path: self.source_picker.set_value(path)
            )
        if self.opened_source_status is not None:
            if state.vaults:
                self.opened_source_status.setText(
                    f"已验证 {len(state.vaults)} 个当前打开仓库"
                    + (f" · {len(state.issues)} 个问题" if state.issues else "")
                )
            else:
                detail = state.issues[0].message if state.issues else "没有仓库被标记为打开。"
                self.opened_source_status.setText(detail)
            self.opened_source_status.setToolTip("\n".join(
                f"{issue.message}{' · ' + issue.path if issue.path else ''}"
                for issue in state.issues
            ))
        if not state.vaults:
            self.set_status(
                state.issues[0].message if state.issues
                else "Obsidian 当前没有可选的已打开仓库。",
                "warning",
            )
            return
        self.set_status("请选择一个已验证的当前打开仓库作为来源。", "success")
        if self.isVisible() and self.opened_source_button is not None:
            self.opened_source_menu.popup(
                self.opened_source_button.mapToGlobal(
                    self.opened_source_button.rect().bottomLeft()
                )
            )

    @Slot()
    def _start_catalog(self) -> None:
        if not self.root_picker.value or not self.source_picker.value:
            return
        root = self.root_picker.value
        self.catalog = None
        self.target_model.set_rows(())
        self._invalidate_plan("正在重新发现仓库。")

        def operation(cancel, progress):
            open_state = self._read_open_state(cancel, progress, validate=False)
            catalog = discover_vaults(root, cancel=cancel, progress=progress)
            return CatalogTaskResult(catalog, open_state)

        self.start_task("catalog", operation)

    @Slot()
    def _start_analyze(self) -> None:
        if not self.target_model.checked_vaults() or not self.selected_component_ids():
            return
        request = self._build_request()
        state_dir = self.state_dir
        self._invalidate_plan("正在分析，仓库内容尚未修改。")

        def operation(cancel, progress):
            open_state = self._read_open_state(cancel, progress, validate=False)
            plan = DeploymentEngine(state_dir).analyze(
                request, cancel=cancel, progress=progress
            )
            return AnalyzeTaskResult(plan, open_state)

        self.start_task("analyze", operation)

    @Slot()
    def _start_execute(self) -> None:
        if self._external_recovery_pending or self.has_pending_recovery():
            self.set_status("另一个待恢复事务尚未处理，已阻止开始新的部署。", "warning")
            return
        if self.plan is None or not self.plan.needs_deploy or not self.confirm_checkbox.isChecked():
            return
        plan = self.plan
        state_dir = self.state_dir
        occupancy_override = not self.occupancy_confirm.isHidden() and self.occupancy_confirm.isChecked()
        target_paths = tuple(item.target_root for item in plan.targets)
        blocker = QSignalBlocker(self.confirm_checkbox)
        self.confirm_checkbox.setChecked(False)
        del blocker

        def operation(cancel, progress):
            open_state = self._read_open_state(cancel, progress, validate=False)
            open_targets = self._open_selected_targets(open_state, target_paths)
            if open_targets:
                raise DeploymentError(
                    "所选目标正被 Obsidian 标记为打开，请关闭 Obsidian 后重新发现并分析："
                    + "；".join(open_targets)
                )
            if not open_state.reliable and not occupancy_override:
                detail = open_state.issues[0].message if open_state.issues else "原因未知"
                raise DeploymentError(
                    "无法自动确认目标仓库是否被 Obsidian 占用；"
                    f"请检查后重新分析并显式确认已关闭目标仓库。（{detail}）"
                )
            result = DeploymentEngine(state_dir).execute(
                plan, cancel=cancel, progress=progress
            )
            return ExecuteTaskResult(result, open_state)

        self.start_task("execute", operation)

    @Slot()
    def _start_rollback(self) -> None:
        if self.current_batch is None or self.current_batch.status not in _ROLLBACK_STATUSES:
            return
        batch_id = self.current_batch.batch_id
        state_dir = self.state_dir
        self._reset_finalize_confirmation()

        def operation(_cancel, _progress):
            return DeploymentEngine(state_dir).rollback(batch_id)

        self.start_task("rollback", operation)

    @Slot()
    def _start_finalize(self) -> None:
        if (self.current_batch is None
                or self.current_batch.status not in _FINALIZE_STATUSES
                or not self.finalize_confirm.isChecked()):
            return
        batch_id = self.current_batch.batch_id
        state_dir = self.state_dir
        self._reset_finalize_confirmation()

        def operation(_cancel, _progress):
            return DeploymentEngine(state_dir).finalize(batch_id)

        self.start_task("finalize", operation)

    def _accept_catalog(self, result: VaultCatalogResult) -> None:
        self.catalog = result
        usable = tuple(vault for vault in result.vaults if not self._source_overlaps_vault(vault))
        excluded = len(result.vaults) - len(usable)
        self.target_model.set_rows(usable)
        self.target_summary.setText(
            f"已发现 {len(usable):,} 个真实仓库 · 已选 0 个"
            + (f" · 已排除 {excluded} 个来源仓库" if excluded else "")
        )
        if result.issues:
            first = result.issues[0]
            self.catalog_issues.setText(
                f"发现过程有 {len(result.issues):,} 个问题；首个：{first.message}"
            )
            self.catalog_issues.setToolTip("\n".join(
                f"{issue.message}{' · ' + issue.path if issue.path else ''}"
                for issue in result.issues
            ))
        else:
            self.catalog_issues.setText("")
            self.catalog_issues.setToolTip("")
        if not usable:
            self.set_status("没有找到可作为目标的真实仓库；来源仓库不会列为目标。", "warning")
        elif result.issues:
            self.set_status(
                f"已发现 {len(usable)} 个可用目标；有 {len(result.issues)} 个路径问题。",
                "warning",
            )
        else:
            self.set_status(
                f"已发现 {len(usable)} 个目标仓库；默认均未选择。", "success"
            )

    def _accept_plan(self, plan: DeploymentPlan) -> None:
        self.plan = plan
        rows: list[DeploymentPreviewRow] = []
        counts = {name: 0 for name in _ACTION_TEXT}
        delete_files = 0
        delete_dirs = 0
        for target in plan.targets:
            target_name = Path(target.target_root).name or target.target_root
            for change in target.changes:
                counts[change.action] = counts.get(change.action, 0) + 1
                if change.action == "delete":
                    if getattr(change, "kind", "file") in {"dir", "directory", "folder"}:
                        delete_dirs += 1
                    else:
                        delete_files += 1
                rows.append(DeploymentPreviewRow(
                    action=change.action,
                    component_id=target.component_id,
                    target_name=target_name,
                    target_root=target.target_root,
                    absolute_path=_absolute_change_path(target.target_path, change.relative_path),
                    size=change.size,
                    reason=change.reason,
                    kind=getattr(change, "kind", "file"),
                ))
        action_order = {"delete": 0, "update": 1, "add": 2, "skip": 3}
        rows.sort(key=lambda item: (
            action_order.get(item.action, 9), item.target_root.casefold(),
            item.component_id.casefold(), item.absolute_path.casefold(), item.absolute_path,
        ))
        self.preview_model.set_rows(rows)
        self.preview_summary.setText(
            f"新增 {counts['add']:,} · 更新 {counts['update']:,} · "
            f"删除 {counts['delete']:,}（{delete_files:,} 文件 · {delete_dirs:,} 文件夹） · "
            f"跳过 {counts['skip']:,}"
        )
        blocker = QSignalBlocker(self.confirm_checkbox)
        self.confirm_checkbox.setChecked(False)
        del blocker
        self.confirm_checkbox.setText(
            f"我确认：来源只读；仅修改 {len(plan.targets):,} 个组件/目标组合；"
            f"将删除目标内 {delete_files:,} 个文件、{delete_dirs:,} 个文件夹。"
        )
        self.confirm_checkbox.setEnabled(plan.needs_deploy)
        if plan.needs_deploy:
            self.set_status(
                f"预览已就绪：将删除 {delete_files} 个目标文件、{delete_dirs} 个目标文件夹；"
                "勾选确认后才能部署。",
                "warning" if counts["delete"] else "success",
            )
        else:
            self.set_status("所有选定组件已经一致，不需要写入或生成备份。", "success")
        self._update_occupancy_display(
            reset_confirmation=True,
            target_paths=(item.target_root for item in plan.targets),
        )
        open_targets = self._open_selected_targets(
            self.open_state, (item.target_root for item in plan.targets)
        )
        if open_targets:
            self.set_status(
                f"已阻止部署：{len(open_targets)} 个所选目标正被 Obsidian 标记为打开。"
                "关闭后重新发现并分析。",
                "error",
            )
        elif not self.open_state.reliable:
            self.set_status(
                "文件与目录预览已完成，但无法自动确认 Obsidian 占用状态；"
                "确认已关闭所选目标后才能部署。",
                "warning",
            )

    def _reset_finalize_confirmation(self) -> None:
        blocker = QSignalBlocker(self.finalize_confirm)
        self.finalize_confirm.setChecked(False)
        del blocker

    def _batch_choice_text(self, batch: JournalBatch) -> str:
        status = _STATUS_TEXT.get(batch.status, batch.status)
        updated = datetime.fromtimestamp(batch.updated_at).strftime("%m-%d %H:%M")
        legacy = " · 旧版/未知入口" if batch.label != self.batch_label else ""
        return f"{batch.batch_id[:8]} · {status}{legacy} · {updated}"

    @staticmethod
    def _batch_tooltip(batch: JournalBatch) -> str:
        updated = datetime.fromtimestamp(batch.updated_at).strftime("%Y-%m-%d %H:%M:%S")
        notes = tuple(dict.fromkeys((
            *batch.errors,
            *(warning for target in batch.targets for warning in target.residuals),
        )))
        targets = "\n".join(
            f"{target.component_id} → {target.target_path}"
            for target in batch.targets
        )
        return (
            f"批次：{batch.batch_id}\n状态：{batch.status}\n"
            f"来源标签：{batch.label or '旧版（无标签）'}\n"
            f"组件/目标：{len(batch.targets)}\n更新时间：{updated}"
            + ("\n目标：\n" + targets if targets else "")
            + ("\n记录：" + "\n".join(notes) if notes else "")
        )

    def _show_current_batch(self) -> None:
        batch = self.current_batch
        if batch is None:
            self.batch_status.setText("没有待处理批次")
            self.batch_status.setToolTip("")
            return
        status = _STATUS_TEXT.get(batch.status, batch.status)
        updated = datetime.fromtimestamp(batch.updated_at).strftime("%Y-%m-%d %H:%M:%S")
        prefix = "" if batch.status in _PENDING_STATUSES else "最近已完成 · "
        self.batch_status.setText(
            f"{prefix}{status} · {batch.batch_id[:8]} · "
            f"{len(batch.targets)} 个组件/目标 · {updated}"
        )
        self.batch_status.setToolTip(self._batch_tooltip(batch))

    @Slot(int)
    def _batch_selection_changed(self, index: int) -> None:
        self._reset_finalize_confirmation()
        batch_id = self.batch_selector.itemData(index) if index >= 0 else None
        self.current_batch = next(
            (batch for batch in self._batches if batch.batch_id == batch_id), None
        )
        self._show_current_batch()
        self.refresh_actions()

    def _reload_persistent_batches(self) -> None:
        selected_id = self.current_batch.batch_id if self.current_batch is not None else None
        self._reset_finalize_confirmation()
        try:
            all_batches = DeploymentEngine(self.state_dir).list_batches()
            batches = tuple(batch for batch in all_batches if (
                batch.label == self.batch_label
                or (
                    self.page_key == _LEGACY_RECOVERY_PAGE
                    and batch.label not in _KNOWN_BATCH_LABELS
                )
            ))
        except (OSError, JournalError) as exc:
            self._batches = ()
            self._pending_batches = ()
            self.current_batch = None
            self._recovery_blocked = True
            blocker = QSignalBlocker(self.batch_selector)
            self.batch_selector.clear()
            del blocker
            self.batch_status.setText(f"无法读取持久批次：{exc}")
            self.batch_status.setToolTip(str(exc))
            return
        self._recovery_blocked = False
        self._batches = batches
        self._pending_batches = tuple(
            batch for batch in batches if batch.status in _PENDING_STATUSES
        )
        choices = self._pending_batches or batches[:1]
        blocker = QSignalBlocker(self.batch_selector)
        self.batch_selector.clear()
        for batch in choices:
            self.batch_selector.addItem(self._batch_choice_text(batch), batch.batch_id)
            index = self.batch_selector.count() - 1
            self.batch_selector.setItemData(index, self._batch_tooltip(batch),
                                            Qt.ItemDataRole.ToolTipRole)
        wanted_index = self.batch_selector.findData(selected_id) if selected_id else -1
        self.batch_selector.setCurrentIndex(wanted_index if wanted_index >= 0 else 0)
        del blocker
        if not batches:
            self.current_batch = None
            self._show_current_batch()
            return
        selected = self.batch_selector.currentData()
        self.current_batch = next(
            (batch for batch in batches if batch.batch_id == selected), choices[0]
        )
        self._show_current_batch()

    def _accept_operation_result(self, kind: str, result: DeploymentResult) -> None:
        self._reload_persistent_batches()
        detail = result.errors[0] if result.errors else ""
        if kind == "execute":
            self._invalidate_plan("部署结束；再次部署前必须重新分析。")
            if result.success:
                self.set_status(
                    f"部署完成：提交 {result.committed_targets} 个目标。"
                    "备份仍保留，请选择确认保留或撤销。",
                    "success",
                )
            else:
                self.set_status(
                    f"部署未完成：{detail or result.journal_status or result.status}", "error"
                )
        elif kind == "rollback":
            if result.success:
                if result.journal_status == "rolled_back_with_residuals":
                    self.set_status(
                        f"撤销完成：恢复 {result.rolled_back_targets} 个目标；"
                        "未获删除授权的空目录已保留，不再阻塞其他任务。",
                        "warning",
                    )
                else:
                    self.set_status(
                        f"撤销完成：恢复 {result.rolled_back_targets} 个目标。", "success"
                    )
            else:
                self.set_status(f"撤销未完成：{detail or result.journal_status}", "error")
        elif kind == "finalize":
            if result.success:
                self.set_status("已确认保留部署结果，事务备份已安全清理。", "success")
            else:
                self.set_status(f"备份清理未完成：{detail or result.journal_status}", "error")
        self.message_logged.emit(self.status_label.text())

    def task_finished(self, status: str, payload: Any) -> None:
        kind = self._task_kind
        super().task_finished(status, payload)
        if status != "ok":
            if kind in {"analyze", "execute"}:
                self._invalidate_plan(
                    "操作已取消，请重新分析。" if status == "cancelled" else str(payload)
                )
                if status == "error":
                    self.set_status(str(payload), "error")
            self._task_kind = ""
            self.refresh_actions()
            return
        if kind == "opened_sources":
            self._accept_opened_sources(payload)
        elif kind == "catalog":
            self.open_state = payload.open_state
            self._accept_catalog(payload.catalog)
            self._update_occupancy_display(reset_confirmation=True)
        elif kind == "analyze":
            self.open_state = payload.open_state
            self._accept_plan(payload.plan)
        elif kind in {"execute", "rollback", "finalize"}:
            if kind == "execute":
                self.open_state = payload.open_state
                self._accept_operation_result(kind, payload.result)
            else:
                self._accept_operation_result(kind, payload)
        self._task_kind = ""
        self.refresh_actions()

    def refresh_actions(self) -> None:
        if not hasattr(self, "root_picker"):
            return
        available = not self._global_busy and not self._task_active
        pending = bool(self._pending_batches)
        self.root_picker.set_controls_enabled(available)
        self.source_picker.set_controls_enabled(available)
        if self.opened_source_button is not None:
            self.opened_source_button.setEnabled(available)
        for box in self.component_boxes.values():
            box.setEnabled(available)
        self.target_table.setEnabled(available)
        self.catalog_button.setEnabled(
            available and bool(self.root_picker.value and self.source_picker.value)
        )
        selected_targets = bool(self.target_model.checked_vaults())
        selected_components = bool(self.selected_component_ids())
        target_paths = (
            tuple(item.target_root for item in self.plan.targets)
            if self.plan is not None else self._selected_target_paths()
        )
        open_targets = self._open_selected_targets(self.open_state, target_paths)
        occupancy_allowed = (
            not open_targets
            and (self.open_state.reliable or self.occupancy_confirm.isChecked())
        )
        self.analyze_button.setEnabled(
            available and not pending and not self._external_recovery_pending
            and not self._recovery_blocked and self.catalog is not None
            and selected_targets and selected_components
        )
        self.confirm_checkbox.setEnabled(
            available and not pending and not self._external_recovery_pending
            and not self._recovery_blocked and self.plan is not None and self.plan.needs_deploy
            and not open_targets
        )
        self.execute_button.setEnabled(
            available and not pending and not self._external_recovery_pending
            and not self._recovery_blocked and self.plan is not None and self.plan.needs_deploy
            and self.confirm_checkbox.isChecked() and occupancy_allowed
        )
        cancellable = self.task_cancellable()
        self.cancel_button.setVisible(cancellable)
        self.cancel_button.setEnabled(cancellable)
        batch_status = self.current_batch.status if self.current_batch is not None else ""
        self.batch_selector.setEnabled(
            available and not self._recovery_blocked and self.batch_selector.count() > 0
        )
        self.rollback_button.setEnabled(available and batch_status in _ROLLBACK_STATUSES)
        can_finalize = available and batch_status in _FINALIZE_STATUSES
        self.finalize_confirm.setEnabled(can_finalize)
        self.finalize_button.setEnabled(can_finalize and self.finalize_confirm.isChecked())
        self.copy_target_button.setEnabled(
            available and bool(self.target_table.selectionModel().selectedRows())
        )
        self.select_all_targets_button.setEnabled(
            available and bool(self.target_model.rows)
            and len(self.target_model.checked_vaults()) < len(self.target_model.rows)
        )
        self.clear_targets_button.setEnabled(
            available and bool(self.target_model.checked_vaults())
        )
        self.copy_preview_button.setEnabled(
            available and bool(self.preview_table.selectionModel().selectedRows())
        )

    def copy_selected_target_paths(self) -> None:
        paths = [
            self.target_model.rows[index.row()].path
            for index in self.target_table.selectionModel().selectedRows()
        ]
        if paths:
            QApplication.clipboard().setText("\n".join(paths))

    def copy_selected_preview_paths(self) -> None:
        paths = [
            self.preview_model.rows[index.row()].absolute_path
            for index in self.preview_table.selectionModel().selectedRows()
        ]
        if paths:
            QApplication.clipboard().setText("\n".join(paths))


class TemplateSuitePage(DistributionPage):
    page_key = "template_suite"
    source_label = "模板套件根目录"
    source_hint = "选择直接包含 .claude、.claudian、.obsidian、File 的模板目录"
    component_options = (
        ComponentOption("claude", ".claude", ".claude"),
        ComponentOption("claudian", ".claudian", ".claudian"),
        ComponentOption("obsidian", ".obsidian", ".obsidian"),
        ComponentOption("file", "File", "File"),
    )

    def __init__(self, state_dir: Path, settings: dict[str, Any], default_root: str,
                 *, obsidian_config_path: str | Path | None = None,
                 opened_vault_reader: Callable[[str | Path | None], OpenedVaultCandidates]
                 | None = None) -> None:
        super().__init__(
            state_dir, settings, default_root,
            title="模板套件部署",
            description=("从一个明确的模板目录选择组件，按文件与目录预览完整部署到所选仓库；"
                         "目标独有内容会列为删除。"),
            obsidian_config_path=obsidian_config_path,
            opened_vault_reader=opened_vault_reader,
        )

    def _components(self) -> tuple[DeploymentComponent, ...]:
        source = Path(self.source_picker.value)
        options = {item.component_id: item for item in self.component_options}
        return tuple(
            DeploymentComponent.direct(key, source / options[key].relative_path,
                                       options[key].relative_path)
            for key in self.selected_component_ids()
        )


class ObsidianConfigPage(DistributionPage):
    page_key = "obsidian_config"
    source_label = "模板仓库或配置"
    source_hint = "选择模板仓库根目录或 .obsidian 目录"
    supports_opened_sources = True
    component_options = (
        ComponentOption("obsidian", ".obsidian 完整配置", ".obsidian", True),
    )

    def __init__(self, state_dir: Path, settings: dict[str, Any], default_root: str,
                 *, obsidian_config_path: str | Path | None = None,
                 opened_vault_reader: Callable[[str | Path | None], OpenedVaultCandidates]
                 | None = None) -> None:
        super().__init__(
            state_dir, settings, default_root,
            title="Obsidian 配置分发",
            description=("把一个来源 .obsidian 完整克隆到多个所选仓库；"
                         "来源仓库自动排除，目标均默认不勾选。"),
            obsidian_config_path=obsidian_config_path,
            opened_vault_reader=opened_vault_reader,
        )

    def _components(self) -> tuple[DeploymentComponent, ...]:
        if "obsidian" not in self.selected_component_ids():
            return ()
        return (DeploymentComponent.obsidian(self.source_picker.value),)


class TemplaterPage(DistributionPage):
    page_key = "templater"
    source_label = "模板 Templater"
    source_hint = "选择模板仓库、File 目录或 File/Templater 目录"
    supports_opened_sources = True
    component_options = (
        ComponentOption("templater", "File/Templater", "File/Templater", True),
    )

    def __init__(self, state_dir: Path, settings: dict[str, Any], default_root: str,
                 *, obsidian_config_path: str | Path | None = None,
                 opened_vault_reader: Callable[[str | Path | None], OpenedVaultCandidates]
                 | None = None) -> None:
        super().__init__(
            state_dir, settings, default_root,
            title="Templater 分发",
            description=("只完整克隆 File/Templater 子树，不触碰 File 下的其他内容；"
                         "支持持久撤销与确认保留。"),
            obsidian_config_path=obsidian_config_path,
            opened_vault_reader=opened_vault_reader,
        )

    def _components(self) -> tuple[DeploymentComponent, ...]:
        if "templater" not in self.selected_component_ids():
            return ()
        return (DeploymentComponent.templater(self.source_picker.value),)


__all__ = [
    "DeploymentPreviewRow",
    "DeploymentPreviewTableModel",
    "DistributionPage",
    "ObsidianConfigPage",
    "TemplateSuitePage",
    "TemplaterPage",
    "VaultTargetTableModel",
]
