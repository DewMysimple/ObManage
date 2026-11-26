from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSignalBlocker,
    QSortFilterProxyModel,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from ..management import read_opened_vault_candidates
from ..management.deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentError,
    DeploymentPlan,
    DeploymentResult,
    RecoveryPreview,
    TERMINAL_BATCH_STATUSES,
)
from ..management.journal import JournalBatch, JournalError
from ..management.comsync import components, newest_vaults, source_root
from ..management.comsync_targets import DirectoryBinding, TargetCatalog, scan_target_catalog
from ..management.running_vaults import RuntimeSnapshot, read_runtime_snapshot
from ..management.comsync_workflow import (
    ComSyncOutcome, ComSyncRequest, ComSyncTarget, SyncApproval, preview, synchronize,
)
from ..management.models import (
    ManagementIssue,
    OpenedVaultCandidates,
    VaultCatalogResult,
    VaultInfo,
)
from ..models import Progress, SyncCancelled
from ..tasking import ConfirmableOperation, TaskConfirmation
from ..paths import canonical, native
from .common import FeaturePage, PathPicker, format_bytes, panel
from .collection_root import CollectionRootPicker
from .icons import action_icon
from .controls import DropDownCombo


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

_COMPONENT_TITLES = {"claude": ".claude", "claudian": ".claudian", "obsidian": ".obsidian",
                     "templater": ".Templater", "file_note": ".File", "file_attachment": ".File"}

_FINALIZE_STATUSES = frozenset({"resolved"})
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
_PENDING_STATUSES = (_FINALIZE_STATUSES | _ROLLBACK_STATUSES) - TERMINAL_BATCH_STATUSES

_STATUS_TEXT = {
    "preparing": "准备中断，建议撤销恢复",
    "prepared": "已准备，尚待恢复",
    "committing": "提交中断，请检查恢复方案",
    "commit_failed": "提交失败，请检查恢复方案",
    "committed": "已同步，等待确认完成或确认回退",
    "failed": "部署失败，等待恢复",
    "prepare_failed": "准备失败，目标未提交",
    "cancelled": "已取消，目标未提交",
    "recovery_required": "自动恢复未完成，需要继续撤销",
    "rolling_back": "撤销中断，需要继续撤销",
    "rollback_required": "撤销未完成，需要继续撤销",
    "rollback_blocked": "回退受阻，可检查后保全回退或保留现状",
    "rolled_back": "已撤销",
    "rolled_back_with_residuals": "已撤销，保留未授权的残留目录",
    "finalizing": "确认清理中断，可继续确认或撤销",
    "finalize_required": "备份清理未完成，可重试确认或撤销",
    "finalized": "已确认保留",
    "resolved": "已结束 · 保留现状，不再阻塞",
    "discarded": "已确认删除记录，待重试",
}


def _physical_key(path: str) -> str:
    value = canonical(path)
    try:
        value = canonical(os.path.realpath(native(value)))
    except OSError:
        pass
    return os.path.normcase(value).casefold()


def _keys_overlap(first: str, second: str) -> bool:
    try:
        common = os.path.commonpath((first, second))
    except ValueError:
        return False
    return common in (first, second)


def _target_id(path: str) -> str:
    digest = hashlib.sha256(_physical_key(path).encode("utf-8")).hexdigest()
    return f"vault-{digest[:20]}"


def _absolute_change_path(target_root: str, relative_path: str) -> str:
    parts = relative_path.replace("\\", "/").split("/")
    return canonical(os.path.join(target_root, *parts))


def _confirmation_prompt(approval: SyncApproval) -> TaskConfirmation:
    request = approval.request
    counts = Counter(change.action for target in approval.plan.targets for change in target.changes)
    detail = [
        f"来源：{request.source}",
        f"组件：{'、'.join(request.component_names)}",
        "",
        f"写入目标（{len(approval.write_targets)} 个）",
        *approval.write_targets,
    ]
    if approval.skipped:
        detail.extend(("", f"跳过目标（{len(approval.skipped)} 个）", *approval.skipped))
    if approval.closed_by_task:
        detail.extend(("", "将恢复本轮关闭的原有会话", *approval.closed_by_task))
    if request.open_after_sync:
        detail.extend(("", "同步成功后，若所选仓库尚未运行，将请求打开。"))
    else:
        detail.extend(("", "仅恢复本轮关闭的原有会话。"))
    return TaskConfirmation(
        "确认本轮配置与模板同步",
        f"所选 {len(request.selected_targets)} 个仓库，实际写入 {len(approval.write_targets)} 个。"
        f"新增 {counts['add']} 项，更新 {counts['update']} 项，删除 {counts['delete']} 项。",
        "\n".join(detail),
        payload=approval,
    )


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
    """Saved registry state used by the existing manual transaction rollback."""

    paths: tuple[str, ...] = ()
    issues: tuple[ManagementIssue, ...] = ()
    reliable: bool = True


@dataclass(frozen=True)
class CatalogTaskResult:
    version: int
    targets: TargetCatalog
    running: RuntimeSnapshot


@dataclass(frozen=True)
class RunningTargetsResult:
    version: int
    running: RuntimeSnapshot


class TargetScope(IntEnum):
    ALL = 0
    RUNNING = 1
    LATEST = 2


class VaultTargetTableModel(QAbstractTableModel):
    """Visible, eligible targets and manually checked paths."""

    checked_changed = Signal()
    HEADERS = ("选择", "仓库", "完整位置")

    def __init__(self, parent=None, *, creation_time=False) -> None:
        super().__init__(parent)
        self.creation_time = creation_time
        self.rows: list[VaultInfo] = []
        self._checked: set[str] = set()

    def set_rows(self, rows: Iterable[VaultInfo], *, retain: set[str] | None = None) -> None:
        selected = self._checked
        self.beginResetModel()
        self.rows = list(rows)
        self._checked = {item.path for item in self.rows} & selected & (retain or set())
        self.endResetModel()
        if selected != self._checked:
            self.checked_changed.emit()

    def checked_vaults(self) -> tuple[VaultInfo, ...]:
        return tuple(item for item in self.rows if item.path in self._checked)

    def set_all_checked(self, checked: bool) -> None:
        wanted = {item.path for item in self.rows} if checked else set()
        self._set_checked(wanted)

    def _set_checked(self, wanted: set[str]) -> None:
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
        return 0 if parent.isValid() else len(self.HEADERS) + int(self.creation_time)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return "创建时间" if section == 3 else self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        item = self.rows[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return (Qt.CheckState.Checked if item.path in self._checked
                    else Qt.CheckState.Unchecked)
        if role == Qt.ItemDataRole.DisplayRole:
            created = getattr(item, "created_ns", None)
            return ("", item.name, item.path,
                    datetime.fromtimestamp(created / 1e9).strftime("%Y-%m-%d %H:%M:%S")
                    if created is not None else "无法读取")[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            nested = f"\n嵌套于：{item.parent_path}" if item.parent_path else ""
            return f"{item.path}{nested}"
        if role == Qt.ItemDataRole.UserRole:
            return ("", item.name.casefold(), item.path.casefold(),
                    getattr(item, "created_ns", None) or 0)[index.column()]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value: Any,
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if (not index.isValid() or not 0 <= index.row() < len(self.rows) or index.column() != 0
                or role != Qt.ItemDataRole.CheckStateRole):
            return False
        key = self.rows[index.row()].path
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
            _COMPONENT_TITLES.get(item.component_id, item.component_id),
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
    compact_sync = False

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
        self._reviewed: tuple[ComSyncRequest, DeploymentPlan] | None = None
        self.catalog: VaultCatalogResult | None = None
        self._catalog_root_binding: DirectoryBinding | None = None
        self._catalog_bindings: dict[str, DirectoryBinding] = {}
        self._eligible_vaults: tuple[VaultInfo, ...] = ()
        self._target_catalog_version = 0
        self._target_scope = TargetScope.ALL
        self._running_targets = RuntimeSnapshot()
        self._hidden_target_count = 0
        self.current_batch: JournalBatch | None = None
        self.recovery_preview: RecoveryPreview | None = None
        self._batches: tuple[JournalBatch, ...] = ()
        self._pending_batches: tuple[JournalBatch, ...] = ()
        self._recovery_blocked = False
        self._request_version = 0
        self._finishing_sync = False
        self._restoring = False

        self.source_frame, config = panel(self.body)
        if self.compact_sync:
            title = QLabel("1  选择来源")
            title.setObjectName("SectionTitle")
            config.addWidget(title)
        self.root_picker = CollectionRootPicker()
        self.root_picker.edit.setObjectName(f"{self.page_key}_root")
        if not self.compact_sync:
            config.addWidget(self.root_picker)
        self.source_picker = PathPicker(
            self.source_label, "", dialog_title=self.source_hint
        )
        self.source_picker.edit.setObjectName(f"{self.page_key}_source")
        self.source_picker.edit.setPlaceholderText(self.source_hint)
        config.addWidget(self.source_picker)
        self.source_picker.selection.config_path = self.obsidian_config_path
        self.source_picker.selection.roots = lambda: (self.root_picker.value,)

        self.component_frame, component_layout = panel(self.body)
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
        self.component_frame.setVisible(not self.compact_sync)

        target_frame, target_layout = panel(self.body)
        target_header = QHBoxLayout()
        target_title = QLabel("2  勾选目标仓库" if self.compact_sync else "目标仓库")
        target_title.setObjectName("SectionTitle")
        target_header.addWidget(target_title)
        self.target_summary = QLabel("可见 0 · 有效 0 · 已选 0 · 请刷新仓库")
        self.target_summary.setObjectName("Muted")
        self.target_summary.setWordWrap(True)
        target_header.addWidget(self.target_summary, 1)
        target_layout.addLayout(target_header)
        if self.compact_sync:
            target_layout.addWidget(self.root_picker)
        target_controls = QHBoxLayout()
        self.target_filter: QLineEdit | None = None
        if self.compact_sync:
            self.target_filter = QLineEdit()
            self.target_filter.setPlaceholderText("搜索目标仓库…")
            self.target_filter.setAccessibleName("搜索目标仓库")
            self.target_filter.setClearButtonEnabled(True)
            self.target_filter.addAction(action_icon("search"), QLineEdit.ActionPosition.LeadingPosition)
            target_controls.addWidget(self.target_filter, 1)
            self.target_scope_buttons = QButtonGroup(self)
            for scope, label in ((TargetScope.ALL, "全部仓库"), (TargetScope.RUNNING, "当前运行"),
                                 (TargetScope.LATEST, "最新创建")):
                button = QPushButton(label)
                button.setCheckable(True)
                button.setProperty("pathAction", True)
                button.setStyleSheet(
                    "QPushButton:checked { background: #EAF2F6; color: #254E68; border-color: #60859B; }"
                )
                self.target_scope_buttons.addButton(button, scope)
                target_controls.addWidget(button)
            self.target_scope_buttons.button(TargetScope.ALL).setChecked(True)
            self.target_scope_buttons.idClicked.connect(self._change_target_scope)
        else:
            target_controls.addStretch()
        self.copy_target_button = QPushButton("复制所选路径", self)
        self.copy_target_button.setObjectName("TextButton")
        if not self.compact_sync:
            target_controls.addWidget(self.copy_target_button)
        else:
            self.copy_target_button.hide()
        self.select_all_targets_button = QPushButton("全选当前列表" if self.compact_sync else "全选目标")
        self.select_all_targets_button.setObjectName("TextButton")
        self.select_all_targets_button.setToolTip("勾选当前可见的全部目标仓库。")
        target_controls.addWidget(self.select_all_targets_button)
        self.clear_targets_button = QPushButton("清空选择")
        self.clear_targets_button.setObjectName("TextButton")
        target_controls.addWidget(self.clear_targets_button)
        self.catalog_button = QPushButton("刷新仓库")
        if self.compact_sync:
            self.root_picker.input_layout.addWidget(self.catalog_button)
        else:
            target_controls.addWidget(self.catalog_button)
        target_layout.addLayout(target_controls)
        self.target_table = QTableView()
        self.target_table.setObjectName(f"{self.page_key}_targets")
        self.target_model = VaultTargetTableModel(self, creation_time=self.page_key == "comsync")
        self.target_proxy = QSortFilterProxyModel(self)
        self.target_proxy.setSourceModel(self.target_model)
        self.target_proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.target_table.setModel(self.target_proxy)
        self.target_table.setSortingEnabled(True)
        self.target_table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        if self.target_filter is not None:
            self.target_filter.textChanged.connect(self._filter_targets)
        self.target_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.target_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.target_table.verticalHeader().hide()
        self.target_table.setMinimumHeight(150 if self.compact_sync else 100)
        self.target_table.setMaximumHeight(180 if self.compact_sync else 150)
        self.target_table.setWordWrap(False)
        target_header_view = self.target_table.horizontalHeader()
        target_header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        target_header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.target_table.setColumnWidth(1, 165)
        target_header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        target_header_view.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        target_layout.addWidget(self.target_table)
        self.catalog_issues = QLabel("")
        self.catalog_issues.setObjectName("Muted")
        self.catalog_issues.setWordWrap(True)
        self.catalog_issues.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        target_layout.addWidget(self.catalog_issues)
        self.target_scope_status = QLabel()
        self.target_scope_status.setObjectName("Muted")
        self.target_scope_status.setWordWrap(True)
        self.target_scope_status.hide()
        target_layout.addWidget(self.target_scope_status)
        self.catalog_issues.hide()

        self.occupancy_status = QLabel("占用状态：尚未检查")
        self.occupancy_status.setObjectName("Muted")
        self.occupancy_status.setWordWrap(True)
        self.occupancy_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        target_layout.addWidget(self.occupancy_status)

        preview_frame, preview_layout = panel(self.body)
        preview_header = QHBoxLayout()
        preview_title = QLabel("3  预览变更" if self.compact_sync else "文件与目录预览")
        preview_title.setObjectName("SectionTitle")
        preview_header.addWidget(preview_title)
        self.preview_summary = QLabel("新增 — · 更新 — · 删除：尚未分析 · 跳过 —")
        self.preview_summary.setObjectName("Muted")
        if self.compact_sync:
            preview_header.addStretch()
        else:
            preview_header.addWidget(self.preview_summary, 1)
        self.copy_preview_button = QPushButton("复制所选完整路径", self)
        self.copy_preview_button.setObjectName("TextButton")
        if self.compact_sync:
            self.copy_preview_button.hide()
        else:
            preview_header.addWidget(self.copy_preview_button)
        preview_layout.addLayout(preview_header)
        if self.compact_sync:
            self.preview_summary.setWordWrap(True)
            preview_layout.addWidget(self.preview_summary)
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
        preview_header_view.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.preview_table.setMinimumHeight(155)
        if self.compact_sync:
            self.preview_table.setColumnHidden(1, True)
            preview_header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
            self.preview_table.setColumnWidth(2, 150)
        preview_layout.addWidget(self.preview_table, 1)
        self.preview_empty = QLabel("勾选目标后即可同步，也可先预览差异。\n新增、更新和删除的实际位置在此显示。", self.preview_table)
        self.preview_empty.setObjectName("Muted")
        self.preview_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.preview_table.viewport())
        empty_layout.addWidget(self.preview_empty)
        self.preview_empty.setVisible(self.compact_sync)
        self.body.setStretch(self.body.count() - 1, 1)

        self.recovery_frame, recovery_layout = panel(self.body)
        recovery_row = QHBoxLayout()
        recovery_title = QLabel("部署事务")
        recovery_title.setObjectName("SectionTitle")
        recovery_row.addWidget(recovery_title)
        self.batch_selector = DropDownCombo()
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
        self.inspect_button = QPushButton("检查恢复方案")
        recovery_layout.addWidget(self.inspect_button)
        self.recovery_details = QPlainTextEdit()
        self.recovery_details.setReadOnly(True)
        self.recovery_details.setMinimumHeight(140)
        self.recovery_details.setMaximumHeight(190)
        self.recovery_details.setPlaceholderText("先检查所选事务，查看目标状态、回退方案和备份位置。")
        recovery_layout.addWidget(self.recovery_details)
        self.resolve_confirm = QCheckBox("接受当前状态（可能仅部分完成），保留全部副本")
        recovery_layout.addWidget(self.resolve_confirm)
        self.resolve_button = QPushButton("保留现状并结束事务")
        recovery_layout.addWidget(self.resolve_button)
        self.preserve_confirm = QCheckBox("已检查方案并关闭目标仓库，保全当前内容后回退")
        recovery_layout.addWidget(self.preserve_confirm)
        recovery_actions = QHBoxLayout()
        self.rollback_button = QPushButton("保全内容后回退")
        self.rollback_button.setObjectName("Danger")
        recovery_actions.addWidget(self.rollback_button)
        self.finalize_button = QPushButton("清理所选事务备份")
        recovery_layout.addLayout(recovery_actions)
        self.finalize_confirm = QCheckBox(
            "我确认永久清理所选批次备份（完成后不能撤销）"
        )
        self.finalize_confirm.setObjectName(f"{self.page_key}_finalize_confirm")
        recovery_layout.addWidget(self.finalize_confirm)
        recovery_layout.addWidget(self.finalize_button)
        self.body.removeWidget(self.recovery_frame)
        self.body.insertWidget(0, self.recovery_frame)

        actions = QHBoxLayout()
        self.safety_hint = QLabel("分析只读取来源和目标；执行只修改已勾选仓库中的选定子树。")
        self.safety_hint.setObjectName("Muted")
        self.safety_hint.setWordWrap(True)
        actions.addWidget(self.safety_hint, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Cancel")
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        self.analyze_button = QPushButton("预览变更" if self.compact_sync else "分析文件与目录差异")
        actions.addWidget(self.analyze_button)
        self.execute_button = QPushButton("开始同步" if self.compact_sync else "开始部署")
        self.execute_button.setObjectName("Primary")
        actions.addWidget(self.execute_button)
        self.root_layout.addLayout(actions)
        if self.compact_sync:
            self.safety_hint.setText("仅同步 .obsidian；目标独有配置会删除，笔记与附件不受影响。")
            for button, icon in (
                (self.source_picker.browse, "folder"), (self.root_picker.browse, "folder"),
                (self.catalog_button, "refresh"), (self.analyze_button, "search"),
                (self.select_all_targets_button, "check"), (self.clear_targets_button, "clear"),
                (self.rollback_button, "undo"),
            ):
                button.setIcon(action_icon(icon))
            self.execute_button.setIcon(action_icon("arrow", "#FFFFFF"))
            self.source_picker.browse.setText("浏览…")
            for table, copier in ((self.target_table, self.copy_selected_target_paths),
                                  (self.preview_table, self.copy_selected_preview_paths)):
                table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                table.customContextMenuRequested.connect(
                    lambda point, table=table, copier=copier: self._path_menu(table, point, copier)
                )

        self.restore_settings(settings)
        self.root_picker.changed.connect(self._collection_changed)
        self.source_picker.changed.connect(self._source_changed)
        for box in self.component_boxes.values():
            box.toggled.connect(self._inputs_changed)
        self.target_model.checked_changed.connect(self._targets_changed)
        self.target_table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_actions()
        )
        self.preview_table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_actions()
        )
        self.catalog_button.clicked.connect(self._start_catalog)
        self.analyze_button.clicked.connect(self._start_analyze)
        self.execute_button.clicked.connect(self._start_execute)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.rollback_button.clicked.connect(self._start_rollback)
        self.inspect_button.clicked.connect(self._start_inspect_recovery)
        self.resolve_button.clicked.connect(self._start_resolve)
        self.resolve_confirm.toggled.connect(lambda *_: self.refresh_actions())
        self.preserve_confirm.toggled.connect(lambda *_: self.refresh_actions())
        self.finalize_button.clicked.connect(self._start_finalize)
        self.batch_selector.currentIndexChanged.connect(self._batch_selection_changed)
        self.finalize_confirm.toggled.connect(lambda *_: self.refresh_actions())
        self.copy_target_button.clicked.connect(self.copy_selected_target_paths)
        self.select_all_targets_button.clicked.connect(
            self._select_visible_targets
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
        self._clear_target_catalog()
        self._invalidate_plan("设置已恢复，请刷新仓库并选择目标。")

    def selected_component_ids(self) -> tuple[str, ...]:
        return tuple(key for key, box in self.component_boxes.items() if box.isChecked())

    def _components(self) -> tuple[DeploymentComponent, ...]:
        raise NotImplementedError

    @Slot()
    def _inputs_changed(self, *_: Any) -> None:
        if self._restoring:
            return
        self._invalidate_plan("输入已改变，已清除上次预览。")
        self._update_occupancy_display()
        self.settings_changed.emit()
        self.refresh_actions()

    def _clear_target_catalog(self) -> None:
        self._target_catalog_version += 1
        self.catalog = None
        self._catalog_root_binding = None
        self._catalog_bindings.clear()
        self._eligible_vaults = ()
        self._running_targets = RuntimeSnapshot()
        self.catalog_issues.clear()
        self.catalog_issues.setToolTip("")
        self.catalog_issues.hide()
        self._replace_target_rows(retain=set())

    @Slot()
    def _collection_changed(self) -> None:
        if self._restoring:
            return
        self._clear_target_catalog()
        self._inputs_changed()

    @Slot()
    def _source_changed(self) -> None:
        if self._restoring:
            return
        self._update_eligible_targets()
        self._replace_target_rows(retain=set())
        self._inputs_changed()

    @Slot()
    def _targets_changed(self) -> None:
        if self._restoring:
            return
        self._invalidate_plan("目标选择已改变，已清除上次预览。")
        self._hidden_target_count = 0
        self._update_target_summary()
        self._update_occupancy_display()
        self.refresh_actions()

    def _filter_targets(self, _text: str) -> None:
        self._replace_target_rows(notify_removed=True)

    def _update_eligible_targets(self) -> None:
        candidates = self.catalog.vaults if self.catalog is not None else ()
        source = self.source_picker.value
        if not candidates or not source:
            self._eligible_vaults = candidates
            return
        source_key = _physical_key(str(source_root(source)))
        self._eligible_vaults = tuple(vault for vault in candidates
            if not _keys_overlap(source_key, os.path.normcase(vault.path).casefold()))

    def _restore_target_scope_button(self) -> None:
        self.target_scope_buttons.button(self._target_scope).setChecked(True)

    @Slot(int)
    def _change_target_scope(self, scope: int) -> None:
        if self._global_busy or self._task_active:
            self._restore_target_scope_button()
            return
        if scope == TargetScope.RUNNING and self.catalog is not None:
            version = self._target_catalog_version
            config = self.obsidian_config_path
            self._restore_target_scope_button()

            def operation(cancel, progress):
                progress(Progress("running_targets", "正在核验当前运行的 Obsidian 仓库…"))
                return RunningTargetsResult(version, read_runtime_snapshot(config, cancel=cancel))

            self.start_task("running_targets", operation)
        else:
            self._target_scope = TargetScope(scope)
            self._replace_target_rows(notify_removed=True)

    def _accept_running_targets(self, payload: RunningTargetsResult) -> None:
        if payload.version == self._target_catalog_version:
            self._running_targets = payload.running
            self._target_scope = TargetScope.RUNNING
            self._replace_target_rows(notify_removed=True)
            self.set_status("已刷新当前运行范围。")
        self._restore_target_scope_button()

    def _replace_target_rows(self, *, retain: set[str] | None = None,
                             notify_removed: bool = False) -> None:
        rows, issue_messages = self._scoped_targets(self._eligible_vaults)
        query = self.target_filter.text().strip().casefold() if self.target_filter else ""
        if query:
            rows = tuple(vault for vault in rows
                         if query in vault.name.casefold() or query in vault.path.casefold())
        selected = {vault.path for vault in self.target_model.checked_vaults()}
        retain = selected if retain is None else retain
        remaining = {vault.path for vault in rows} & retain
        if tuple(self.target_model.rows) != rows or selected - remaining:
            self.target_model.set_rows(rows, retain=retain)
        self._hidden_target_count = len(selected - remaining) if notify_removed else 0
        self.target_scope_status.setText(issue_messages[0] if issue_messages else "")
        self.target_scope_status.setToolTip("\n".join(issue_messages))
        self.target_scope_status.setVisible(bool(issue_messages))
        self._update_target_summary()
        self.refresh_actions()

    def _scoped_targets(self, eligible: tuple[VaultInfo, ...]) -> tuple[tuple[VaultInfo, ...], tuple[str, ...]]:
        if self.catalog is None:
            return (), ()
        if self._target_scope == TargetScope.LATEST:
            latest = newest_vaults(eligible)
            if eligible and not latest:
                paths = tuple(vault.path for vault in eligible if getattr(vault, "created_ns", None) is None)
                return (), ("无法确定最新创建仓库：部分有效候选的创建时间不可读取。", *paths)
            return latest, ()
        if self._target_scope == TargetScope.RUNNING:
            running = {os.path.normcase(session.vault.path): session.directory_identity
                       for session in self._running_targets.sessions}
            rows, changed = [], []
            for vault in eligible:
                key = os.path.normcase(vault.path)
                if key in running:
                    binding = self._catalog_bindings.get(vault.path)
                    if binding is not None and binding.identity == running[key]:
                        rows.append(vault)
                    else:
                        changed.append(vault.path)
            issues = [issue.message for issue in self._running_targets.issues
                      if issue.code != "runtime_not_found"]
            if changed:
                issues.extend(("部分运行仓库的目录身份已改变，请刷新仓库。", *changed))
            if issues:
                return tuple(rows), (f"运行识别不完整：{issues[0]}", *issues[1:])
            return tuple(rows), (() if rows else ("当前集合内未识别到正在运行的目标仓库。",))
        return eligible, ()

    def _update_target_summary(self) -> None:
        eligible = len(self._eligible_vaults)
        suffix = " · 请刷新仓库" if self.catalog is None else ""
        if self._hidden_target_count:
            suffix = f" · 已移除 {self._hidden_target_count} 个不再可用或可见的选择"
        self.target_summary.setText(
            f"可见 {len(self.target_model.rows)} · 有效 {eligible} · "
            f"已选 {len(self.target_model.checked_vaults())}{suffix}"
        )

    def _select_visible_targets(self) -> None:
        self.target_model.set_all_checked(True)

    def _path_menu(self, table, point, copier) -> None:
        if not table.selectionModel().selectedRows():
            return
        menu = QMenu(table)
        menu.addAction("复制所选完整路径", copier)
        menu.exec(table.viewport().mapToGlobal(point))

    def _invalidate_plan(self, message: str = "输入已改变，可预览或直接同步。") -> None:
        self._request_version += 1
        if self._task_active and self._task_kind in {"analyze", "execute"}:
            self.cancel_requested.emit()
        self._reset_recovery_preview()
        self._reset_finalize_confirmation()
        self.plan = None
        self._reviewed = None
        self.preview_model.set_rows(())
        self.preview_empty.setVisible(self.compact_sync)
        self.preview_summary.setText("新增 — · 更新 — · 删除：尚未分析 · 跳过 —")
        if not self._task_active:
            self.set_status(message)

    def invalidate_confirmation(self) -> None:
        if self._reviewed is not None or (self._task_active and self._task_kind == "analyze"):
            self._invalidate_plan()
        self._reset_recovery_preview()
        blocker = QSignalBlocker(self.finalize_confirm)
        self.finalize_confirm.setChecked(False)
        del blocker
        self.refresh_actions()

    def has_pending_recovery(self) -> bool:
        return self._recovery_blocked or bool(self._pending_batches)

    def task_cancellable(self) -> bool:
        return self._task_active and not self._finishing_sync and self._task_kind in {
            "catalog", "running_targets", "analyze", "execute"
        }

    def task_progress(self, event: Progress) -> None:
        if event.phase in {"recovering-deployment", "restoring-sessions"}:
            self._finishing_sync = True
            self.refresh_actions()
        super().task_progress(event)

    def _read_open_state(self, cancel, progress) -> OpenVaultState:
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

    def _update_occupancy_display(self, **_kwargs) -> None:
        selected_count = len(self._selected_target_paths())
        open_hint = ("成功后若未运行，请求打开所选仓库。" if selected_count == 1
                     else "恢复本轮关闭的会话。")
        self.occupancy_status.setText("同步会正常关闭需要写入的目标仓库窗口。" + open_hint)
        self.occupancy_status.setToolTip(
            "请先保存编辑内容。关闭失败时停止本轮同步；其他仓库可以继续运行。")

    @Slot()
    def _start_catalog(self) -> None:
        if self._global_busy or self._task_active or not self.root_picker.value:
            return
        root = self.root_picker.value
        version = self._target_catalog_version
        check_running = self._target_scope == TargetScope.RUNNING
        config = self.obsidian_config_path

        def operation(cancel, progress):
            targets = scan_target_catalog(root, cancel, progress)
            running = read_runtime_snapshot(config, cancel=cancel) if check_running else RuntimeSnapshot()
            return CatalogTaskResult(version, targets, running)

        self.start_task("catalog", operation)

    def _freeze_request(self) -> ComSyncRequest:
        assert self._catalog_root_binding is not None
        source = str(source_root(self.source_picker.value))
        paths = self._selected_target_paths()
        return ComSyncRequest(
            version=self._request_version,
            source=source,
            collection=self._catalog_root_binding,
            selected_targets=tuple(
                ComSyncTarget(_target_id(path), self._catalog_bindings[path])
                for path in paths
            ),
            component_names=tuple(self.component_boxes[key].text() for key in self.selected_component_ids()),
            components=self._components(),
            label=self.batch_label,
            open_after_sync=len(paths) == 1,
        )

    @Slot()
    def _start_analyze(self) -> None:
        if not self.analyze_button.isEnabled():
            return
        self._invalidate_plan("正在分析，仓库内容尚未修改。")
        request = self._freeze_request()
        state_dir = self.state_dir

        def operation(cancel, progress):
            return request, preview(state_dir, request, cancel, progress)

        self.start_task("analyze", operation)

    @Slot()
    def _start_execute(self) -> None:
        if self._external_recovery_pending or self.has_pending_recovery():
            self.set_status("存在待处理或无法读取的事务，已阻止新的同步。", "warning")
            return
        if not self.execute_button.isEnabled():
            return
        reviewed = self._reviewed
        self._reviewed = None
        if reviewed is None:
            self._invalidate_plan("正在准备本轮同步。")
        request = self._freeze_request()
        state_dir, config_path = self.state_dir, self.obsidian_config_path

        def operation(cancel, progress, confirm):
            return synchronize(state_dir, config_path, request, cancel, progress,
                               lambda approval: confirm(_confirmation_prompt(approval)), reviewed=reviewed)

        self.start_task("execute", ConfirmableOperation(operation))

    def task_confirmation(self, prompt: TaskConfirmation) -> bool:
        approval = prompt.payload
        if not isinstance(approval, SyncApproval) or approval.request.version != self._request_version:
            return False
        self._accept_plan(approval.plan)
        self.set_status("等待确认本轮同步。可以查看最终差异或取消。", "warning")
        return True

    @Slot()
    def _start_inspect_recovery(self) -> None:
        if self.current_batch is None:
            return
        batch_id, state_dir = self.current_batch.batch_id, self.state_dir
        self._reset_recovery_preview()
        self._reset_finalize_confirmation()
        self.start_task("inspect_recovery", lambda _cancel, _progress:
                        DeploymentEngine(state_dir).inspect_recovery(batch_id))

    @Slot()
    def _start_resolve(self, *, confirmed: bool = False) -> None:
        preview = self.recovery_preview
        if (preview is None or not (confirmed or self.resolve_confirm.isChecked()) or self.current_batch is None
                or self.current_batch.status not in _PENDING_STATUSES):
            return
        state_dir = self.state_dir
        self._reset_recovery_preview()
        self.start_task("resolve", lambda _cancel, _progress: DeploymentEngine(state_dir).resolve(
            preview.batch_id, expected_revision=preview.revision))

    @Slot()
    def _start_rollback(self, *, confirmed: bool = False) -> None:
        preview = self.recovery_preview
        if (preview is None or not preview.can_rollback or not (confirmed or self.preserve_confirm.isChecked())
                or self.current_batch is None or self.current_batch.status not in _ROLLBACK_STATUSES):
            return
        state_dir = self.state_dir
        target_paths = tuple(target.target_root for target in self.current_batch.targets)
        self._reset_recovery_preview()

        def operation(cancel, progress):
            def check_open(paths, *, report=None):
                opened = self._read_open_state(None, report)
                if self._open_selected_targets(opened, paths):
                    raise DeploymentError("目标仓库仍在 Obsidian 中打开，请关闭后重新检查。")

            if cancel.is_set():
                raise SyncCancelled("已取消读取 Obsidian 当前打开仓库。")
            check_open(target_paths, report=progress)
            return DeploymentEngine(state_dir).rollback(
                preview.batch_id, preview=preview, safety_check=check_open
            )

        self.start_task("rollback", operation)

    @Slot()
    def _start_finalize(self) -> None:
        preview = self.recovery_preview
        if (self.current_batch is None
                or self.current_batch.status not in _FINALIZE_STATUSES
                or preview is None or preview.cleanup_error
                or not self.finalize_confirm.isChecked()):
            return
        batch_id = self.current_batch.batch_id
        state_dir = self.state_dir
        self._reset_recovery_preview()
        self._reset_finalize_confirmation()

        def operation(_cancel, _progress):
            return DeploymentEngine(state_dir).finalize(batch_id, preview=preview)

        self.start_task("finalize", operation)

    def _accept_catalog_task(self, payload: CatalogTaskResult) -> None:
        if (payload.version != self._target_catalog_version
                or payload.targets.root != self.root_picker.value):
            return
        root_binding = payload.targets.root_binding
        incoming = {binding.path: binding for binding in payload.targets.bindings}
        retain = set()
        if root_binding == self._catalog_root_binding:
            retain = {path for path, binding in incoming.items()
                      if binding == self._catalog_bindings.get(path)}
        self._catalog_root_binding = root_binding
        self._catalog_bindings = incoming
        self._running_targets = payload.running
        self._accept_catalog(payload.targets.catalog, retain=retain)

    def _accept_catalog(self, result: VaultCatalogResult, *, retain: set[str] | None = None) -> None:
        self.catalog = result
        self._update_eligible_targets()
        self._replace_target_rows(retain=retain or set(), notify_removed=True)
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
        self.catalog_issues.setVisible(bool(result.issues))
        usable = len(self._eligible_vaults)
        if result.issues:
            self.set_status(
                f"已发现 {usable} 个可用目标，另有 {len(result.issues)} 个发现问题。",
                "warning",
            )
        elif not usable:
            self.set_status("没有找到可作为目标的真实仓库。", "warning")
        else:
            self.set_status(f"已发现 {usable} 个可用目标。", "success")

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
        self.preview_empty.hide()
        self.preview_summary.setText(
            f"新增 {counts['add']:,} · 更新 {counts['update']:,} · "
            f"删除 {counts['delete']:,}（{delete_files:,} 文件 · {delete_dirs:,} 文件夹） · "
            f"跳过 {counts['skip']:,}"
        )
        if plan.needs_deploy:
            self.set_status(
                f"预览完成。将删除 {delete_files} 个目标文件和 {delete_dirs} 个目标文件夹。",
                "warning" if counts["delete"] else "success",
            )
        else:
            self.set_status("所有选定组件在本次分析中一致。", "success")

    def _reset_finalize_confirmation(self) -> None:
        blocker = QSignalBlocker(self.finalize_confirm)
        self.finalize_confirm.setChecked(False)
        del blocker

    def _reset_recovery_preview(self) -> None:
        self.recovery_preview = None
        self.recovery_details.clear()
        for checkbox in (self.resolve_confirm, self.preserve_confirm):
            blocker = QSignalBlocker(checkbox)
            checkbox.setChecked(False)
            del blocker

    def _batch_choice_text(self, batch: JournalBatch) -> str:
        status = _STATUS_TEXT.get(batch.status, batch.status)
        updated = datetime.fromtimestamp(batch.updated_at).strftime("%m-%d %H:%M")
        return f"{batch.batch_id[:8]} · {status} · {updated}"

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
            f"来源标签：{batch.label or '无标签'}\n"
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
        self._reset_recovery_preview()
        self._reset_finalize_confirmation()
        batch_id = self.batch_selector.itemData(index) if index >= 0 else None
        self.current_batch = next(
            (batch for batch in self._batches if batch.batch_id == batch_id), None
        )
        self._show_current_batch()
        self.refresh_actions()

    def _reload_persistent_batches(self) -> None:
        self._reset_recovery_preview()
        selected_id = self.current_batch.batch_id if self.current_batch is not None else None
        self._reset_finalize_confirmation()
        try:
            all_batches = DeploymentEngine(self.state_dir).list_batches()
            batches = all_batches
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
        choices = self._pending_batches + tuple(batch for batch in batches if batch not in self._pending_batches)
        if self._pending_batches and selected_id not in {batch.batch_id for batch in self._pending_batches}:
            selected_id = self._pending_batches[0].batch_id
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
        if kind == "rollback":
            if result.success:
                if result.journal_status == "rolled_back_with_residuals":
                    self.set_status(
                        f"撤销完成：恢复 {result.rolled_back_targets} 个目标；"
                        "保全内容及残留目录可在事务处理页查看，不再阻塞其他任务。",
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
                self.set_status("事务备份已安全清理；当前仓库及回退时保全的内容保持不变。", "success")
            else:
                self.set_status(f"备份清理未完成：{detail or result.journal_status}", "error")
        elif kind == "resolve":
            if result.success:
                self.set_status("本事务已结束；仓库现状及全部副本保持不变。其他待处理事务仍需分别处理。", "success")
            else:
                self.set_status(f"事务未结束：{detail}", "error")
        for message in result.errors:
            self.message_logged.emit(message)
        self.message_logged.emit(self.status_label.text())

    def _accept_sync_result(self, outcome: ComSyncOutcome) -> None:
        self._reload_persistent_batches()
        self._invalidate_plan("本轮同步已结束。")
        result = outcome.execution
        details = list(outcome.skipped)
        errors = list(result.errors if result is not None else ())
        if outcome.error:
            errors.append(outcome.error)
        details.extend(errors)
        if outcome.status == "success":
            summary = f"同步完成，提交 {result.committed_targets} 个组件/仓库组合。请到事务处理页确认完成或回退。"
        elif outcome.status == "no_changes":
            summary = "无需写入。"
        elif outcome.status == "cancelled":
            summary = "本轮同步已取消。"
        else:
            summary = "同步停止。" + (errors[0] if errors else "")
        if result is not None and result.journal_status and outcome.status != "success":
            state = _STATUS_TEXT.get(result.journal_status, result.journal_status)
            summary += f"事务状态为{state}。"
        if outcome.skipped:
            summary += f"跳过 {len(outcome.skipped)} 个仓库。"
        for restore, label in ((True, "会话恢复"), (False, "显式打开")):
            openings = [item for item in outcome.openings if item.restore == restore]
            if not openings:
                continue
            states = {"requested": "已请求打开", "already_running": "已在运行",
                      "failed": "打开失败", "held": "保持关闭"}
            for item in openings:
                details.append(f"{label} · {states[item.status]} · {item.target} {item.error}")
            summary += label + "：" + "、".join(
                f"{text} {num} 个"
                for status, text in states.items()
                if (num := sum(item.status == status for item in openings))
            ) + "。"
        warning = any(item.error for item in outcome.openings)
        kind = ("error" if outcome.status == "failed" else
                "warning" if warning or outcome.status == "cancelled" else "success")
        self.set_status(summary, kind)
        self.status_label.setToolTip("\n".join(details))
        for message in (*details, summary):
            self.message_logged.emit(message)

    def task_finished(self, status: str, payload: Any) -> None:
        kind = self._task_kind
        self._finishing_sync = False
        super().task_finished(status, payload)
        if status != "ok":
            if kind == "running_targets":
                self._restore_target_scope_button()
        elif kind == "catalog":
            self._accept_catalog_task(payload)
        elif kind == "running_targets":
            self._accept_running_targets(payload)
        elif kind == "analyze":
            request, plan = payload
            if request.version == self._request_version:
                self._accept_plan(plan)
                self._reviewed = request, plan
        elif kind == "execute":
            self._accept_sync_result(payload)
        elif kind == "inspect_recovery":
            self.recovery_preview = payload
            self.recovery_details.setPlainText("\n\n".join(payload.details))
            self.set_status("检查完成；请选择确认完成或确认回退。")
        elif kind in {"rollback", "finalize", "resolve"}:
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
        for box in self.component_boxes.values():
            box.setEnabled(available)
        self.target_table.setEnabled(available)
        self.catalog_button.setEnabled(
            available and bool(self.root_picker.value)
            and (self.compact_sync or bool(self.source_picker.value))
        )
        target_paths = self._selected_target_paths()
        valid_request = (
            available and self._catalog_root_binding is not None
            and bool(target_paths) and bool(self.selected_component_ids())
            and bool(self.source_picker.value)
            and all(path in self._catalog_bindings for path in target_paths)
        )
        self.analyze_button.setEnabled(valid_request)
        self.execute_button.setEnabled(
            valid_request and not pending and not self._external_recovery_pending
            and not self._recovery_blocked
        )
        cancellable = self.task_cancellable()
        self.cancel_button.setVisible(cancellable)
        self.cancel_button.setEnabled(cancellable)
        batch_status = self.current_batch.status if self.current_batch is not None else ""
        self.batch_selector.setEnabled(
            available and not self._recovery_blocked and self.batch_selector.count() > 0
        )
        self.inspect_button.setEnabled(available and self.current_batch is not None)
        inspected = self.recovery_preview is not None
        can_resolve = available and inspected and batch_status in _PENDING_STATUSES
        self.resolve_confirm.setEnabled(can_resolve)
        self.resolve_button.setEnabled(can_resolve and self.resolve_confirm.isChecked())
        self.recovery_details.setVisible(inspected)
        self.resolve_confirm.setVisible(inspected and batch_status in _PENDING_STATUSES)
        self.resolve_button.setVisible(inspected and batch_status in _PENDING_STATUSES)
        can_preserve = (available and inspected and self.recovery_preview.can_rollback
                        and batch_status in _ROLLBACK_STATUSES)
        self.preserve_confirm.setEnabled(can_preserve)
        self.rollback_button.setEnabled(can_preserve and self.preserve_confirm.isChecked())
        self.preserve_confirm.setVisible(inspected and batch_status in _ROLLBACK_STATUSES)
        self.rollback_button.setVisible(inspected and batch_status in _ROLLBACK_STATUSES)
        can_finalize = (available and batch_status in _FINALIZE_STATUSES and inspected
                        and not self.recovery_preview.cleanup_error)
        self.finalize_confirm.setEnabled(can_finalize)
        self.finalize_button.setEnabled(can_finalize and self.finalize_confirm.isChecked())
        self.finalize_confirm.setVisible(inspected and batch_status in _FINALIZE_STATUSES
                                         and not getattr(self, "recovery_detached", False))
        self.finalize_button.setVisible(inspected and batch_status in _FINALIZE_STATUSES
                                        and not getattr(self, "recovery_detached", False))
        if getattr(self, "recovery_detached", False):
            self.inspect_button.hide()
            self.resolve_confirm.hide()
            self.preserve_confirm.hide()
            self.resolve_button.setEnabled(can_resolve)
            self.rollback_button.setEnabled(can_preserve)
            self.rollback_button.setToolTip(self.recovery_preview.rollback_error if inspected else "正在检查事务")
            self.recovery_details.setVisible(inspected and self.recovery_details_expanded)
        self.copy_target_button.setEnabled(
            available and bool(self.target_table.selectionModel().selectedRows())
        )
        self.select_all_targets_button.setEnabled(
            available and any(
                self.target_proxy.index(row, 0).data(Qt.ItemDataRole.CheckStateRole)
                != Qt.CheckState.Checked for row in range(self.target_proxy.rowCount())
            )
        )
        self.clear_targets_button.setEnabled(
            available and bool(self.target_model.checked_vaults())
        )
        self.copy_preview_button.setEnabled(
            available and bool(self.preview_table.selectionModel().selectedRows())
        )
        if self.compact_sync:
            self.recovery_frame.setVisible(
                not getattr(self, "recovery_detached", False)
                and (pending or self._recovery_blocked or batch_status in {"rolled_back_with_residuals", "resolved"})
            )
            self._update_occupancy_display()
            self.occupancy_status.setVisible(bool(target_paths))
            self.progress_bar.setVisible(self._task_active)
            if self.target_filter is not None:
                self.target_filter.setEnabled(available)
            self.analyze_button.setObjectName("Primary" if self.plan is None else "")
            self.analyze_button.style().unpolish(self.analyze_button)
            self.analyze_button.style().polish(self.analyze_button)
            self.analyze_button.setIcon(action_icon("search", "#FFFFFF" if self.plan is None else "#476C82"))

    def copy_selected_target_paths(self) -> None:
        paths = [
            self.target_model.rows[self.target_proxy.mapToSource(index).row()].path
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


class ComSyncPage(DistributionPage):
    page_key = "comsync"
    source_label = "配置来源"
    source_hint = "选择来源仓库或模板套件根目录"
    compact_sync = True
    component_options = (
        ComponentOption("claude", ".claude", ".claude"),
        ComponentOption("claudian", ".claudian", ".claudian"),
        ComponentOption("obsidian", ".obsidian", ".obsidian", True),
        ComponentOption("templater", ".Templater", "File/Templater"),
        ComponentOption("file", ".File", "File"),
    )

    def __init__(self, state_dir: Path, settings: dict[str, Any], default_root: str,
                 *, obsidian_config_path: str | Path | None = None,
                 opened_vault_reader=None) -> None:
        super().__init__(state_dir, settings, default_root,
            title=".comSync",
            description="统一同步配置和模板；为新仓库补齐 File 目录结构。",
            obsidian_config_path=obsidian_config_path, opened_vault_reader=opened_vault_reader)
        # Keep component controls together with the source, without another card.
        self.component_frame.setVisible(True)
        self.source_frame.layout().addWidget(self.component_frame)
        self.component_frame.setObjectName("ComponentOptions")
        self.component_frame.layout().setContentsMargins(0, 0, 0, 0)
        grid = self.component_frame.layout().itemAt(0).layout()
        caption = grid.itemAtPosition(0, 0).widget()
        grid.removeWidget(caption)
        caption.setText("同步组件")
        grid.addWidget(caption, 0, 0, 2, 1)
        for index, box in enumerate(self.component_boxes.values()):
            grid.removeWidget(box)
        for index, box in enumerate(self.component_boxes.values()):
            grid.addWidget(box, index // 3, index % 3 + 1)
        grid.setColumnStretch(3, 0)
        grid.setColumnStretch(4, 1)
        self._component_grid, self._component_caption = grid, caption
        self.component_boxes["file"].setToolTip(
            "补齐 File/Note、File/Attachment 空目录，保留已有内容；同时完整同步 File/Templater。")
        self.component_boxes["templater"].setToolTip(
            "完整同步 File/Templater 内容；选择 .File 时已包含，不会重复同步。")
        self.component_scope.setText(
            ".File 包含 .Templater 内容；Note、Attachment 只补齐目录，已有笔记和附件保留。")
        self.safety_hint.setText("选中配置和模板按来源完整同步，目标独有内容会进入删除预览。")
        self.execute_button.setToolTip(
            "分析并正常关闭需要写入的目标会话，检查最终变更后确认同步。")
        self.preview_table.setColumnHidden(1, False)
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.preview_table.setColumnWidth(1, 95)
        self.target_table.setMinimumHeight(115)
        self.target_table.setMaximumHeight(145)
        self.preview_table.setMinimumHeight(125)
        self.target_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.target_table.setColumnWidth(3, 164)
        self.target_scope_buttons.button(TargetScope.LATEST).setToolTip(
            "按全部有效候选的目录创建时间筛选；并列项全部显示。")
        self.refresh_actions()

    def restore_settings(self, settings: dict[str, Any]) -> None:
        super().restore_settings(settings)
        self._include_templater()

    def _include_templater(self) -> None:
        if self.component_boxes["file"].isChecked():
            blocker = QSignalBlocker(self.component_boxes["templater"])
            self.component_boxes["templater"].setChecked(True)
            del blocker

    def _inputs_changed(self, *_: Any) -> None:
        self._include_templater()
        super()._inputs_changed()

    def refresh_actions(self) -> None:
        super().refresh_actions()
        if hasattr(self, "execute_button"):
            self.execute_button.setText(
                "同步并打开" if len(self._selected_target_paths()) == 1 else "同步所选")
        if hasattr(self, "component_boxes") and "templater" in self.component_boxes:
            self.component_boxes["templater"].setEnabled(
                not self._global_busy and not self._task_active and not self.component_boxes["file"].isChecked())
        for button in self.target_scope_buttons.buttons():
            button.setEnabled(not self._global_busy and not self._task_active)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.target_table.setMaximumHeight(280 if self.height() > 1100 else 145)
        if hasattr(self, "_component_grid"):
            grid = self._component_grid
            grid.removeWidget(self._component_caption)
            for box in self.component_boxes.values():
                grid.removeWidget(box)
            columns = 5 if self.width() >= 930 else 3
            grid.addWidget(self._component_caption, 0, 0, 1 if columns == 5 else 2, 1)
            for index, box in enumerate(self.component_boxes.values()):
                grid.addWidget(box, index // columns, index % columns + 1)
            grid.setColumnStretch(4, 0)
            grid.setColumnStretch(6, 1)

    def _components(self) -> tuple[DeploymentComponent, ...]:
        return components(self.source_picker.value, self.selected_component_ids())


__all__ = ["DeploymentPreviewRow", "DeploymentPreviewTableModel", "DistributionPage",
           "ComSyncPage", "VaultTargetTableModel"]
