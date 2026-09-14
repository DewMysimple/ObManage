from __future__ import annotations

import ntpath
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QSignalBlocker,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableView,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from ..engine import SYNC_MODE_NO_VIDEO, SyncEngine
from ..file_types import VIDEO_EXTENSIONS
from ..models import PlanItem, SyncPlan, SyncResult
from .common import FeaturePage, PathPicker, format_bytes, panel


BACKUP_FILTERS = (
    ("待执行", {"add", "mkdir", "update", "rename", "delete", "rmdir", "error"}),
    ("全部", None),
    ("新增", {"add", "mkdir"}),
    ("更新", {"update", "rename"}),
    ("删除", {"delete", "rmdir"}),
    ("排除视频", {"exclude"}),
    ("跳过", {"skip"}),
    ("异常", {"error"}),
)

_ACTION_COLORS = {
    "add": "#27735B",
    "mkdir": "#27735B",
    "update": "#326B94",
    "rename": "#326B94",
    "delete": "#AD5748",
    "rmdir": "#AD5748",
    "exclude": "#8A6A3D",
    "skip": "#79848B",
    "error": "#AD5748",
}


def _drive_label(path: str) -> str:
    value = path
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    drive = ntpath.splitdrive(value)[0]
    return drive.upper() if len(drive) == 2 and drive[1] == ":" else drive


def _endpoint_label(name: str, path: str) -> str:
    drive = _drive_label(path)
    return f"{name}（{drive}）" if drive else name


def _absolute_path(root: str, relative: str) -> str:
    if not root:
        return relative
    if ntpath.splitdrive(root)[0]:
        return ntpath.normpath(ntpath.join(root, relative.replace("/", "\\")))
    return os.path.abspath(os.path.join(root, *relative.replace("\\", "/").split("/")))


class BackupPlanTableModel(QAbstractTableModel):
    ACTION_COLUMN, TARGET_COLUMN, SIZE_COLUMN, REASON_COLUMN = range(4)
    HEADERS = ("操作", "目标位置", "大小", "说明")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[PlanItem] = []
        self.source = ""
        self.target = ""
        self.source_name = "来源仓库"
        self.target_name = "目标仓库"

    def set_context(
        self, source: str, target: str, source_name: str, target_name: str
    ) -> None:
        self.beginResetModel()
        self.source = source
        self.target = target
        self.source_name = source_name
        self.target_name = target_name
        self.endResetModel()

    def set_rows(self, rows: list[PlanItem]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return None

    def _action_text(self, item: PlanItem) -> str:
        if item.action == "exclude":
            return "排除视频 · 不修改"
        if item.action in {"delete", "rmdir"}:
            return f"仅从{self.target_name}删除"
        if item.action == "skip":
            return "跳过 · 无需修改"
        if item.action == "error":
            return "无法处理"
        verb = {
            "add": "新增",
            "mkdir": "新建文件夹",
            "update": "覆盖更新",
            "rename": "重命名",
        }.get(item.action, item.action)
        return f"在{self.target_name}{verb}"

    def data(
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        item = self.rows[index.row()]
        source_path = _absolute_path(self.source, item.relative_path)
        target_path = _absolute_path(self.target, item.relative_path)
        values = (
            self._action_text(item),
            target_path,
            "—" if item.action in {"mkdir", "rmdir"} else format_bytes(item.size),
            item.reason,
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return item.action
        if role == Qt.ItemDataRole.ToolTipRole:
            effect = (
                "此视频不会复制、覆盖或删除。"
                if item.action == "exclude"
                else f"所有实际修改只发生在{self.target_name}。"
            )
            return (
                f"{values[0]}\n目标位置：{target_path}\n对应来源：{source_path}\n"
                f"{item.reason}\n{effect}\n{self.source_name}只读取。"
            )
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 0:
            return QColor(_ACTION_COLORS.get(item.action, "#273A46"))
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 2:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None


class BackupPlanFilterModel(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.actions: set[str] | None = None

    def set_actions(self, actions: set[str] | None) -> None:
        self.beginFilterChange()
        self.actions = actions
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        if self.actions is None:
            return True
        model = self.sourceModel()
        return model.data(model.index(row, 0, parent), Qt.ItemDataRole.UserRole) in self.actions


class BackupPlanTableView(QTableView):
    """Keep all four preview columns visible at laptop and full-screen widths."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.horizontalHeader()
        header.setMinimumSectionSize(40)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

    def viewportEvent(self, event: Any) -> bool:
        result = super().viewportEvent(event)
        if event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.fit_columns)
        return result

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.fit_columns()

    @Slot()
    def fit_columns(self) -> None:
        try:
            model = self.model()
        except RuntimeError:
            return
        if model is None or model.columnCount() != 4:
            return
        width = self.viewport().width()
        action_width, size_width = 196, 86
        flexible = max(80, width - action_width - size_width)
        path_width = round(flexible * 0.62)
        for column, column_width in enumerate(
            (action_width, path_width, size_width, flexible - path_width)
        ):
            self.horizontalHeader().resizeSection(column, column_width)


class VaultBackupPage(FeaturePage):
    """Two-way vault mirroring that deliberately leaves video files unmanaged."""

    def __init__(
        self,
        state_dir: Path,
        settings: dict[str, Any],
        default_local: str,
        default_portable: str,
    ) -> None:
        super().__init__(
            "仓库备份",
            "在笔记本与移动硬盘之间同步非视频内容；视频不会复制、覆盖或删除。",
        )
        self.state_dir = Path(state_dir)
        self.plan: SyncPlan | None = None
        self.last_result: SyncResult | None = None
        direction = settings.get("direction", "to_local")
        self.direction = direction if direction in {"to_local", "to_portable"} else "to_local"
        local_value = settings.get("local_path", default_local)
        portable_value = settings.get("portable_path", default_portable)
        self._initializing = True

        paths_frame, paths_layout = panel(self.body)
        self.paths_panel = paths_frame
        direction_row = QHBoxLayout()
        direction_caption = QLabel("同步方向")
        direction_caption.setFixedWidth(106)
        direction_row.addWidget(direction_caption)
        self.to_local_button = QPushButton("移动硬盘 → 笔记本")
        self.to_portable_button = QPushButton("笔记本 → 移动硬盘")
        self.direction_group = QButtonGroup(self)
        self.direction_group.setExclusive(True)
        for button in (self.to_local_button, self.to_portable_button):
            button.setObjectName("DirectionChoice")
            button.setCheckable(True)
            self.direction_group.addButton(button)
            direction_row.addWidget(button, 1)
        self.to_local_button.setToolTip(
            "以移动硬盘完整仓库为准，把非视频内容备份到笔记本轻量仓库。"
        )
        self.to_portable_button.setToolTip(
            "以笔记本轻量仓库为准，把非视频改动带回移动硬盘；移动硬盘视频保留。"
        )
        paths_layout.addLayout(direction_row)
        self.local_picker = PathPicker(
            "笔记本轻量仓库",
            local_value if isinstance(local_value, str) else default_local,
            dialog_title="选择笔记本上的无视频 Obsidian 仓库",
        )
        self.local_picker.edit.setObjectName("backup_local_path")
        paths_layout.addWidget(self.local_picker)
        self.portable_picker = PathPicker(
            "移动硬盘完整仓库",
            portable_value if isinstance(portable_value, str) else default_portable,
            dialog_title="选择移动硬盘上的完整 Obsidian 仓库",
        )
        self.portable_picker.edit.setObjectName("backup_portable_path")
        paths_layout.addWidget(self.portable_picker)
        direction_detail = QHBoxLayout()
        direction_detail.addSpacing(116)
        self.direction_label = QLabel()
        self.direction_label.setObjectName("Direction")
        direction_detail.addWidget(self.direction_label)
        self.source_safety_label = QLabel("来源只读取")
        self.source_safety_label.setObjectName("SourceSafety")
        direction_detail.addWidget(self.source_safety_label)
        self.target_effect_label = QLabel("目标仅同步非视频内容（含删除）")
        self.target_effect_label.setObjectName("TargetEffect")
        direction_detail.addWidget(self.target_effect_label)
        direction_detail.addStretch()
        paths_layout.addLayout(direction_detail)
        video_hint = QLabel(
            "视频按扩展名识别并完全排除：来源视频不复制，目标视频不覆盖、不删除；"
            "Markdown、图片、音频、PDF、配置及其他普通文件仍按来源镜像。"
        )
        video_hint.setObjectName("Muted")
        video_hint.setWordWrap(True)
        video_hint.setToolTip(
            "当前排除的视频扩展名：" + "、".join(sorted(VIDEO_EXTENSIONS))
        )
        paths_layout.addWidget(video_hint)

        summary_frame, summary_layout = panel(self.body)
        summary_frame.setObjectName("Panel")
        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(20)
        self.summary_values: dict[str, QLabel] = {}
        for index, (key, title) in enumerate((
            ("add", "新增"),
            ("update", "更新"),
            ("delete", "删除"),
            ("exclude", "排除视频"),
            ("bytes", "待复制"),
        )):
            block = QHBoxLayout()
            caption = QLabel(title)
            caption.setObjectName("Muted")
            value = QLabel("—")
            value.setObjectName("StatValue")
            block.addWidget(caption)
            block.addWidget(value)
            summary_grid.addLayout(block, 0, index)
            summary_grid.setColumnStretch(index, 1)
            self.summary_values[key] = value
        summary_layout.addLayout(summary_grid)

        preview_frame, preview_layout = panel(self.body)
        preview_header = QHBoxLayout()
        self.preview_title = QLabel("差异预览")
        self.preview_title.setObjectName("SectionTitle")
        preview_header.addWidget(self.preview_title)
        preview_header.addStretch()
        self.copy_button = QPushButton("复制所选目标位置")
        self.copy_button.setObjectName("TextButton")
        preview_header.addWidget(self.copy_button)
        preview_layout.addLayout(preview_header)
        self.filter_tabs = QTabBar()
        self.filter_tabs.setExpanding(False)
        self.filter_tabs.setDrawBase(False)
        for name, _actions in BACKUP_FILTERS:
            self.filter_tabs.addTab(name)
        preview_layout.addWidget(self.filter_tabs)
        self.preview_stack = QStackedWidget()
        self.preview_stack.setMinimumHeight(235)
        self.preview_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.addStretch()
        self.empty_title = QLabel("先分析非视频差异")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setStyleSheet("color: #657984; font-size: 16px; font-weight: 500;")
        self.empty_detail = QLabel("选择方向和两端仓库后，分析阶段只读取文件。")
        self.empty_detail.setObjectName("Muted")
        self.empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_detail.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_detail)
        empty_layout.addStretch()
        self.preview_stack.addWidget(empty_page)
        self.table = BackupPlanTableView()
        self.table.setObjectName("backup_preview_table")
        self.model = BackupPlanTableModel(self)
        self.proxy = BackupPlanFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(35)
        self.preview_stack.addWidget(self.table)
        preview_layout.addWidget(self.preview_stack, 1)
        self.body.setStretch(2, 1)

        self.confirm_checkbox = QCheckBox()
        self.confirm_checkbox.setEnabled(False)
        self.confirm_checkbox.setObjectName("backup_confirm_direction")
        self.root_layout.addWidget(self.confirm_checkbox)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Cancel")
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        self.deep_button = QPushButton("完整校验非视频内容")
        self.deep_button.setToolTip("绕过快速记录，重新读取并比较两端全部非视频文件。")
        actions.addWidget(self.deep_button)
        self.analyze_button = QPushButton("分析非视频差异")
        actions.addWidget(self.analyze_button)
        self.execute_button = QPushButton("开始备份")
        self.execute_button.setObjectName("Primary")
        actions.addWidget(self.execute_button)
        self.root_layout.addLayout(actions)

        self.to_local_button.clicked.connect(lambda: self.set_direction("to_local"))
        self.to_portable_button.clicked.connect(lambda: self.set_direction("to_portable"))
        self.local_picker.changed.connect(self._inputs_changed)
        self.portable_picker.changed.connect(self._inputs_changed)
        self.confirm_checkbox.toggled.connect(lambda *_: self.refresh_actions())
        self.filter_tabs.currentChanged.connect(self._filter_changed)
        self.copy_button.clicked.connect(self.copy_selected_target_paths)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: self.refresh_actions()
        )
        self.analyze_button.clicked.connect(lambda: self._start_analyze(False))
        self.deep_button.clicked.connect(lambda: self._start_analyze(True))
        self.execute_button.clicked.connect(self._start_execute)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self._initializing = False
        self._refresh_direction_text()
        self._filter_changed(0)
        self.refresh_actions()

    def settings_payload(self) -> dict[str, Any]:
        return {
            "local_path": self.local_picker.value,
            "portable_path": self.portable_picker.value,
            "direction": self.direction,
        }

    def repository_paths(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.local_picker.value, self.portable_picker.value)
            if value
        )

    def _paths(self) -> tuple[str, str]:
        local = self.local_picker.value
        portable = self.portable_picker.value
        return (portable, local) if self.direction == "to_local" else (local, portable)

    def _endpoint_names(self) -> tuple[str, str]:
        local = _endpoint_label("笔记本", self.local_picker.value)
        portable = _endpoint_label("移动硬盘", self.portable_picker.value)
        return (portable, local) if self.direction == "to_local" else (local, portable)

    @staticmethod
    def _normalized_path(path: str) -> str:
        value = path
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return os.path.normcase(os.path.realpath(os.path.expanduser(value)))

    def _plan_matches_paths(self) -> bool:
        if self.plan is None or self.plan.mode != SYNC_MODE_NO_VIDEO:
            return False
        source, target = self._paths()
        return (
            self._normalized_path(source) == self._normalized_path(self.plan.source)
            and self._normalized_path(target) == self._normalized_path(self.plan.target)
        )

    def _refresh_direction_text(self) -> None:
        to_local = self.direction == "to_local"
        source, target = self._paths()
        source_name, target_name = self._endpoint_names()
        for button, checked in (
            (self.to_local_button, to_local),
            (self.to_portable_button, not to_local),
        ):
            blocker = QSignalBlocker(button)
            button.setChecked(checked)
            del blocker
        self.direction_label.setText(f"{source_name}  →  {target_name}")
        self.direction_label.setToolTip(f"本次只读取：{source}\n本次仅修改非视频内容：{target}")
        self.source_safety_label.setToolTip(f"来源仓库不覆盖、不删除：\n{source}")
        self.target_effect_label.setToolTip(
            f"非视频新增、覆盖和删除只发生在：\n{target}\n目标已有视频始终保留。"
        )
        self.preview_title.setText(f"非视频差异预览 · 仅修改{target_name}")
        self.confirm_checkbox.setText(
            "我已确认本次方向与非视频删除；仅修改目标，视频保持原样"
        )
        self.confirm_checkbox.setToolTip(
            f"只读取：{source}\n仅修改非视频内容：{target}\n"
            "重新分析、切换方向或离开本页后需要再次确认。"
        )
        self.execute_button.setText(f"更新{target_name}")
        self.model.set_context(source, target, source_name, target_name)

    @Slot()
    def _inputs_changed(self, *_: Any) -> None:
        if self._initializing:
            return
        self._invalidate_plan("仓库位置已改变，旧预览与确认已清除。")
        self._refresh_direction_text()
        self.settings_changed.emit()

    def set_direction(self, direction: str) -> None:
        if direction not in {"to_local", "to_portable"}:
            raise ValueError("同步方向无效")
        if self._task_active or self._global_busy:
            self._refresh_direction_text()
            return
        if self.direction == direction:
            self._refresh_direction_text()
            return
        self.direction = direction
        self._invalidate_plan("同步方向已改变，旧预览与确认已清除。")
        self._refresh_direction_text()
        self.settings_changed.emit()

    def _invalidate_plan(self, message: str) -> None:
        self.plan = None
        blocker = QSignalBlocker(self.confirm_checkbox)
        self.confirm_checkbox.setChecked(False)
        del blocker
        self.model.set_rows([])
        self.preview_stack.setCurrentIndex(0)
        self.empty_title.setText("先分析非视频差异")
        self.empty_detail.setText("分析只读取两端；视频文件不会进入复制、更新或删除。")
        for value in self.summary_values.values():
            value.setText("—")
        for index, (name, _actions) in enumerate(BACKUP_FILTERS):
            self.filter_tabs.setTabText(index, name)
        if not self._task_active:
            self.set_status(message)
        self.refresh_actions()

    def invalidate_confirmation(self) -> None:
        was_checked = self.confirm_checkbox.isChecked()
        blocker = QSignalBlocker(self.confirm_checkbox)
        self.confirm_checkbox.setChecked(False)
        del blocker
        if was_checked and self.plan is not None:
            self.set_status("已离开本页面，本轮备份确认已撤销。", "warning")
        self.refresh_actions()

    @Slot(int)
    def _filter_changed(self, index: int) -> None:
        selected = BACKUP_FILTERS[max(0, index)][1]
        self.proxy.set_actions(selected)
        self.preview_stack.setCurrentIndex(1 if self.proxy.rowCount() else 0)
        if self.plan is not None and not self.proxy.rowCount():
            self.empty_title.setText("此分类没有项目")
            self.empty_detail.setText("可切换上方分类查看完整预览和已排除的视频。")
        QTimer.singleShot(0, self.table.fit_columns)

    def _start_analyze(self, deep: bool) -> None:
        source, target = self._paths()
        if not source or not target:
            self.set_status("请先填写笔记本轻量仓库和移动硬盘完整仓库。", "warning")
            return
        self._invalidate_plan("正在分析；此阶段只读取两个仓库。")
        self._refresh_direction_text()
        state_dir = self.state_dir

        def operation(cancel, progress):
            return SyncEngine(state_dir).analyze(
                source,
                target,
                deep=deep,
                mode=SYNC_MODE_NO_VIDEO,
                cancel=cancel,
                progress=progress,
            )

        self.start_task("analyze_deep" if deep else "analyze", operation)
        if not self._task_active:
            return
        source_name, target_name = self._endpoint_names()
        self.set_status(
            f"正在以{source_name}为准分析非视频差异；{target_name}尚未修改。",
            "busy",
        )
        self.message_logged.emit(
            f"开始{'完整校验' if deep else '分析'}无视频备份：{source} → {target}；"
            "视频不复制、不覆盖、不删除。"
        )

    def _start_execute(self) -> None:
        if self._external_recovery_pending:
            self.set_status("存在待恢复事务，已阻止新的仓库备份写入。", "warning")
            return
        if (
            self.plan is None
            or not self._plan_matches_paths()
            or not self.plan.can_execute
            or not self.plan.has_changes
            or not self.confirm_checkbox.isChecked()
        ):
            self.set_status("请先重新分析并确认当前方向与非视频差异预览。", "warning")
            return
        allow_empty = False
        deletions = any(
            item.action in {"delete", "rmdir"} for item in self.plan.items
        )
        if self.plan.source_empty and deletions:
            source_name, target_name = self._endpoint_names()
            answer = QMessageBox.warning(
                self,
                "排除视频后来源为空",
                f"{source_name}排除视频后没有可管理内容。\n\n"
                f"继续会删除{target_name}中预览列出的全部非视频内容；"
                "两端视频仍会保留。确认继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.set_status("已保留目标非视频内容；请核对来源后重新分析。")
                return
            allow_empty = True
        plan = self.plan
        state_dir = self.state_dir
        blocker = QSignalBlocker(self.confirm_checkbox)
        self.confirm_checkbox.setChecked(False)
        del blocker

        def operation(cancel, progress):
            return SyncEngine(state_dir).execute(
                plan,
                allow_empty=allow_empty,
                cancel=cancel,
                progress=progress,
            )

        source_name, target_name = self._endpoint_names()
        self.start_task("execute", operation)
        if not self._task_active:
            return
        self.set_status(
            f"正在更新{target_name}的非视频内容；{source_name}只读取，视频保持原样。",
            "busy",
        )
        self.message_logged.emit(
            f"开始无视频备份：{plan.source} → {plan.target}；"
            f"预计复制 {format_bytes(plan.bytes_to_copy)}，视频保持原样。"
        )

    def _accept_plan(self, plan: SyncPlan) -> None:
        if plan.mode != SYNC_MODE_NO_VIDEO:
            self._invalidate_plan("后台返回了错误的同步模式，请重新分析。")
            self.set_status("后台返回了错误的同步模式，已拒绝预览。", "error")
            return
        self.plan = plan
        if not self._plan_matches_paths():
            self._invalidate_plan("分析期间路径或方向发生变化，请重新分析。")
            self.set_status("分析期间路径或方向发生变化，请重新分析。", "warning")
            return
        source_name, target_name = self._endpoint_names()
        self.model.set_context(plan.source, plan.target, source_name, target_name)
        self.model.set_rows(plan.items)
        counts = plan.counts
        self.summary_values["add"].setText(f"{counts['add'] + counts['mkdir']:,}")
        self.summary_values["update"].setText(
            f"{counts['update'] + counts['rename']:,}"
        )
        self.summary_values["delete"].setText(
            f"{counts['delete'] + counts['rmdir']:,}"
        )
        self.summary_values["exclude"].setText(f"{counts['exclude']:,}")
        self.summary_values["bytes"].setText(format_bytes(plan.bytes_to_copy))
        for index, (name, actions) in enumerate(BACKUP_FILTERS):
            total = (
                len(plan.items)
                if actions is None
                else sum(counts.get(action, 0) for action in actions)
            )
            self.filter_tabs.setTabText(index, f"{name} {total:,}")
        self.filter_tabs.setCurrentIndex(0)
        self._filter_changed(0)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(1000)
        excluded_bytes = max(plan.excluded_source_bytes, plan.excluded_target_bytes)
        if not plan.can_execute:
            detail = plan.errors[0] if plan.errors else "存在无法安全处理的路径。"
            self.set_status(f"本轮无法备份：{detail}", "error")
        elif not plan.has_changes:
            self.set_status(
                f"两端非视频内容已经一致；已排除 {counts['exclude']:,} 个视频"
                f"（约 {format_bytes(excluded_bytes)}）。",
                "success",
            )
        else:
            self.set_status(
                f"预览已就绪：仅修改{target_name}的非视频内容；"
                f"已排除 {counts['exclude']:,} 个视频。",
                "success",
            )
        self.message_logged.emit(
            f"无视频备份分析完成：新增 {counts['add']} 文件 / {counts['mkdir']} 文件夹，"
            f"更新 {counts['update']}，重命名 {counts['rename']}，"
            f"删除 {counts['delete']} 文件 / {counts['rmdir']} 文件夹，"
            f"排除视频 {counts['exclude']}，跳过 {counts['skip']}。"
        )

    def _accept_result(self, result: SyncResult) -> None:
        self.last_result = result
        self.plan = None
        blocker = QSignalBlocker(self.confirm_checkbox)
        self.confirm_checkbox.setChecked(False)
        del blocker
        _source_name, target_name = self._endpoint_names()
        summary = (
            f"复制 {result.copied_files:,} 个文件（{format_bytes(result.copied_bytes)}），"
            f"删除 {result.deleted_files:,} 个文件、{result.deleted_dirs:,} 个文件夹"
        )
        if result.renamed_items:
            summary += f"，重命名 {result.renamed_items:,} 项"
        if result.status == "success":
            self.set_status(
                f"仓库备份完成：{target_name}{summary}；所有视频保持原样。",
                "success",
            )
            self.progress_bar.setValue(1000)
        elif result.status == "cancelled":
            self.set_status(
                f"备份已取消；已完成部分保留，视频未处理。请重新分析。{summary}",
                "warning",
            )
        else:
            detail = result.errors[0] if result.errors else "未知错误"
            self.set_status(
                f"备份未完整完成：{summary}。首个问题：{detail}",
                "error",
            )
        self.message_logged.emit(self.status_label.text())
        self.preview_title.setText(
            f"本轮执行前记录 · {datetime.now():%H:%M:%S} · 视频未处理"
        )

    def task_finished(self, status: str, payload: Any) -> None:
        kind = self._task_kind
        super().task_finished(status, payload)
        if status == "ok":
            if kind.startswith("analyze"):
                self._accept_plan(payload)
            elif kind == "execute":
                self._accept_result(payload)
        elif kind.startswith("analyze"):
            self._invalidate_plan(
                "分析已取消，请重新开始。" if status == "cancelled" else str(payload)
            )
        elif kind == "execute":
            self.plan = None
            self.set_status(
                "备份任务已停止；再次操作前必须重新分析。"
                if status == "cancelled"
                else str(payload),
                "warning" if status == "cancelled" else "error",
            )
        self._task_kind = ""
        self.refresh_actions()

    @Slot()
    def copy_selected_target_paths(self) -> None:
        values = [
            index.siblingAtColumn(BackupPlanTableModel.TARGET_COLUMN).data()
            for index in self.table.selectionModel().selectedRows()
        ]
        values = [str(value) for value in values if value]
        if values:
            QApplication.clipboard().setText("\n".join(values))

    def refresh_actions(self) -> None:
        if not hasattr(self, "local_picker"):
            return
        available = not self._global_busy and not self._task_active
        self.local_picker.set_controls_enabled(available)
        self.portable_picker.set_controls_enabled(available)
        self.to_local_button.setEnabled(available)
        self.to_portable_button.setEnabled(available)
        source, target = self._paths()
        paths_ready = bool(source and target)
        self.analyze_button.setEnabled(available and paths_ready)
        self.deep_button.setEnabled(available and paths_ready)
        executable = (
            available
            and not self._external_recovery_pending
            and self._plan_matches_paths()
            and self.plan is not None
            and self.plan.can_execute
            and self.plan.has_changes
        )
        self.confirm_checkbox.setEnabled(executable)
        self.execute_button.setEnabled(executable and self.confirm_checkbox.isChecked())
        self.cancel_button.setVisible(self._task_active)
        self.cancel_button.setEnabled(self._task_active)
        self.copy_button.setEnabled(
            available and bool(self.table.selectionModel().selectedRows())
        )


__all__ = [
    "BACKUP_FILTERS",
    "BackupPlanTableModel",
    "BackupPlanTableView",
    "VaultBackupPage",
]
