from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRectF,
    QSignalBlocker,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTime,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QTabBar,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .engine import SyncEngine
from .models import PlanItem, Progress, SyncCancelled, SyncPlan, SyncResult
from .scheduler import Scheduler
from .settings import SettingsStore


ACTION_NAMES = {
    "add": "新增文件",
    "update": "更新文件",
    "rename": "重命名",
    "delete": "删除文件",
    "skip": "跳过",
    "mkdir": "新增文件夹",
    "rmdir": "删除文件夹",
    "error": "无法处理",
}
ACTION_COLORS = {
    "add": "#27735B",
    "mkdir": "#27735B",
    "update": "#326B94",
    "rename": "#326B94",
    "delete": "#AD5748",
    "rmdir": "#AD5748",
    "skip": "#79848B",
    "error": "#AD5748",
}
FILTERS = (
    ("全部", None),
    ("新增", {"add", "mkdir"}),
    ("更新", {"update", "rename"}),
    ("删除", {"delete", "rmdir"}),
    ("跳过", {"skip"}),
    ("异常", {"error"}),
)


def format_bytes(value: int | float) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024 or unit == "PB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return "0 B"


def app_icon() -> QIcon:
    """An original, legible document-and-arrow icon for the window and tray."""
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2E526D"))
    painter.drawRoundedRect(QRectF(2, 2, 124, 124), 28, 28)
    painter.setBrush(QColor("#96B6C7"))
    painter.drawRoundedRect(QRectF(28, 27, 51, 62), 7, 7)
    painter.setBrush(QColor("#F4F8FA"))
    painter.drawRoundedRect(QRectF(41, 40, 51, 62), 7, 7)
    painter.setPen(QPen(QColor("#2E526D"), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(54, 71, 80, 71)
    painter.drawLine(72, 62, 81, 71)
    painter.drawLine(72, 80, 81, 71)
    painter.end()
    return QIcon(pixmap)


class PlanTableModel(QAbstractTableModel):
    """Keep a large vault preview in a plain list, without per-cell widgets."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.items: list[PlanItem] = []

    def set_items(self, items: list[PlanItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else 4

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ("操作", "相对路径", "大小", "说明")[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                ACTION_NAMES.get(item.action, item.action),
                item.relative_path,
                "—" if item.action in ("mkdir", "rmdir") else format_bytes(item.size),
                item.reason,
            )[column]
        if role == Qt.ItemDataRole.UserRole:
            return item.action
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{item.relative_path}\n{item.reason}".strip()
        if role == Qt.ItemDataRole.ForegroundRole and column == 0:
            return QColor(ACTION_COLORS.get(item.action, "#273A46"))
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column == 2:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        return None


class PlanFilterModel(QSortFilterProxyModel):
    def __init__(self, parent: QObject | None = None) -> None:
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


class DropDownCombo(QComboBox):
    """Keep the drop-down affordance visible with editable, styled combo boxes."""

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#7B8D96" if self.isEnabled() else "#B6C1C6"), 1.4))
        x, y = self.width() - 14, self.height() // 2
        painter.drawLine(x - 3, y - 2, x, y + 1)
        painter.drawLine(x, y + 1, x + 3, y - 2)
        painter.end()


class SyncWorker(QThread):
    progress = Signal(object)
    completed = Signal(object)

    def __init__(
        self,
        state_dir: Path,
        operation: str,
        source: str,
        target: str,
        deep: bool,
        plan: SyncPlan | None,
        allow_empty: bool,
        cancel: threading.Event,
    ) -> None:
        super().__init__()
        self.state_dir = state_dir
        self.operation = operation
        self.source = source
        self.target = target
        self.deep = deep
        self.plan = plan
        self.allow_empty = allow_empty
        self.cancel = cancel
        self.last_progress = 0.0
        self.last_phase = ""
        self._rate_key: tuple[str, str] | None = None
        self._rate_started = 0.0
        self._rate_initial_bytes = 0
        self._rate_last_bytes = 0
        self._copy_started = 0.0
        self._copy_initial_bytes = 0

    def _progress(self, progress: Progress) -> None:
        # Avoid flooding the GUI event queue when scanning tens of thousands of files.
        now = time.monotonic()
        # Calculate timing before throttling, so a fast source→target hash boundary
        # cannot be missed by the UI and counted as one progressively slower file.
        key = (progress.phase, progress.relative_path)
        if key != self._rate_key or progress.completed_bytes < self._rate_last_bytes:
            self._rate_key = key
            self._rate_started = now
            self._rate_initial_bytes = progress.completed_bytes
        self._rate_last_bytes = progress.completed_bytes
        if progress.phase in ("copy", "copied"):
            if not self._copy_started:
                self._copy_started = now
                self._copy_initial_bytes = progress.completed_bytes
            progress._rate_started = self._copy_started
            progress._rate_initial_bytes = self._copy_initial_bytes
        else:
            progress._rate_started = self._rate_started
            progress._rate_initial_bytes = self._rate_initial_bytes
        if now - self.last_progress >= 0.08 or (progress.phase == "done" and self.last_phase != "done"):
            self.last_progress = now
            self.last_phase = progress.phase
            self.progress.emit(progress)

    @Slot()
    def run(self) -> None:
        try:
            engine = SyncEngine(self.state_dir)
            if self.operation == "analyze":
                result = engine.analyze(
                    self.source,
                    self.target,
                    deep=self.deep,
                    cancel=self.cancel,
                    progress=self._progress,
                )
            else:
                assert self.plan is not None
                result = engine.execute(
                    self.plan,
                    allow_empty=self.allow_empty,
                    cancel=self.cancel,
                    progress=self._progress,
                )
            self.completed.emit(("ok", result))
        except SyncCancelled:
            self.completed.emit(("cancelled", None))
        except Exception as exc:
            self.completed.emit(("error", f"{type(exc).__name__}: {exc}"))


STYLE = """
QMainWindow, QDialog { background: #F3F5F4; }
QWidget { color: #23353F; font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 13px; }
QLabel { background: transparent; }
QLabel#Brand { font-size: 27px; font-weight: 700; letter-spacing: -0.5px; }
QLabel#Subtitle, QLabel#Muted { color: #758189; font-size: 12px; }
QLabel#SectionTitle { font-size: 14px; font-weight: 600; }
QLabel#Badge { background: #E5EBED; color: #48626F; border-radius: 12px; padding: 7px 12px; font-size: 12px; }
QFrame#Panel, QFrame#Stat { background: #FFFFFF; border: 1px solid #DFE5E5; border-radius: 12px; }
QLabel#StatLabel { color: #6A7980; font-size: 12px; }
QLabel#StatValue { font-size: 27px; font-weight: 600; }
QLineEdit, QComboBox, QSpinBox, QTimeEdit { min-height: 32px; border: 1px solid #DAE1E3; border-radius: 6px; background: #FAFBFB; padding: 1px 10px; selection-background-color: #DCE9F0; selection-color: #23353F; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTimeEdit:focus { border-color: #7195AA; background: #FFFFFF; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QTimeEdit:disabled { color: #929CA2; background: #F2F4F4; }
QComboBox::drop-down { width: 22px; border: none; }
QComboBox::down-arrow { image: none; }
QSpinBox::up-button, QTimeEdit::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 20px; height: 17px; border-left: 1px solid #DAE1E3; }
QSpinBox::down-button, QTimeEdit::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; height: 17px; border-left: 1px solid #DAE1E3; }
QComboBox QAbstractItemView { background: #FFFFFF; border: 1px solid #DDE4E5; selection-background-color: #E9F0F3; selection-color: #23353F; padding: 4px; }
QPushButton { background: #FFFFFF; border: 1px solid #D9E1E3; border-radius: 7px; min-height: 34px; padding: 1px 15px; font-weight: 500; }
QPushButton:hover { background: #F1F5F6; border-color: #ADC0C9; }
QPushButton:pressed { background: #E6EDF0; }
QPushButton:disabled { color: #A2AAAD; background: #F1F3F3; border-color: #E3E7E7; }
QPushButton#Primary { background: #315F7B; color: #FFFFFF; border: 1px solid #315F7B; font-weight: 600; padding: 1px 24px; }
QPushButton#Primary:hover { background: #264F69; border-color: #264F69; }
QPushButton#Primary:pressed { background: #203F55; }
QPushButton#Primary:disabled { background: #C6D2D9; border-color: #C6D2D9; color: #F7F9FA; }
QPushButton#TextButton { background: transparent; border-color: transparent; color: #5B7180; padding-left: 3px; }
QPushButton#TextButton:hover { color: #274E69; background: #E9EEEF; }
QPushButton#Cancel { color: #A04F44; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; border: 1px solid #B9C7CD; border-radius: 5px; background: #FFFFFF; }
QCheckBox::indicator:checked { background: #315F7B; border: 4px solid #315F7B; image: none; }
QCheckBox::indicator:disabled { border-color: #D9E0E3; background: #E6ECEF; }
QTabBar { background: transparent; }
QTabBar::tab { color: #78868D; background: transparent; padding: 9px 13px 8px; border-bottom: 2px solid transparent; font-size: 12px; }
QTabBar::tab:selected { color: #315F7B; border-bottom: 2px solid #315F7B; }
QTabBar::tab:hover { background: #F2F6F7; }
QTableView { background: #FFFFFF; border: none; border-top: 1px solid #EBEFEF; selection-background-color: #EAF1F5; selection-color: #23353F; gridline-color: #F0F3F4; outline: none; }
QTableView::item { padding: 0 12px; border-bottom: 1px solid #F0F3F4; }
QTableView::item:selected { background: #EAF1F5; color: #23353F; }
QHeaderView::section { background: #F8FAFA; color: #74818A; border: none; border-bottom: 1px solid #E8EDED; padding: 9px 12px; text-align: left; font-size: 12px; font-weight: 400; }
QProgressBar { min-height: 5px; max-height: 5px; border: none; border-radius: 2px; background: #E8EDEF; }
QProgressBar::chunk { background: #5E8AA2; border-radius: 2px; }
QScrollBar:vertical { border: none; background: #F7F9F9; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #CFD9DC; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { border: none; background: #F7F9F9; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #CFD9DC; border-radius: 4px; min-width: 24px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QPlainTextEdit { border: 1px solid #DAE1E3; border-radius: 7px; background: #FFFFFF; padding: 9px; font-size: 12px; }
QMenu { background: #FFFFFF; border: 1px solid #DAE1E3; padding: 5px; }
QMenu::item { padding: 7px 28px 7px 13px; border-radius: 4px; }
QMenu::item:selected { background: #EAF1F5; }
QToolTip { color: #23353F; background: #FFFFFF; border: 1px solid #CCD7DC; padding: 5px; }
"""


class MainWindow(QMainWindow):
    """Chinese desktop shell; the mirror engine never runs on the GUI thread."""

    def __init__(self, state_dir: Path) -> None:
        super().__init__()
        self.state_dir = Path(state_dir)
        self.store = SettingsStore(self.state_dir)
        self.settings = self.store.load()
        self.scheduler = Scheduler()
        self.plan: SyncPlan | None = None
        self.last_result: SyncResult | None = None
        self._thread: QThread | None = None
        self._worker: SyncWorker | None = None
        self._cancel_event = threading.Event()
        self._pending_outcome: tuple[str, Any] | None = None
        self._operation = ""
        self._scheduled = False
        self._job_started = 0.0
        self._progress_key: tuple[str, str] | None = None
        self._progress_started = 0.0
        self._progress_initial_bytes = 0
        self._progress_last_bytes = 0
        self._copy_started = 0.0
        self._copy_initial_bytes = 0
        self._exit_requested = False
        self._confirming_empty = False
        self._tray_close_notified = False
        self._log_dialog: QDialog | None = None
        self._log_lines: list[str] = []
        self._load_log()

        self.setWindowTitle("ObManage · 仓库镜像")
        self.setObjectName("main_window")
        self.setWindowIcon(app_icon())
        self.resize(1140, 890)
        self.setMinimumSize(980, 800)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._build_tray()
        self._connect_signals()
        self.scheduler.configure(self.settings)
        self._refresh_schedule_controls()
        self._refresh_actions()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._timer_tick)
        self._timer.start()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._persist)
        if self.store.last_error:
            self._log(self.store.last_error)
            self._set_status("设置已重置，请检查同步位置。", "warning")

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(16)

        header = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(self.windowIcon().pixmap(44, 44))
        mark.setFixedSize(44, 44)
        header.addWidget(mark)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        brand = QLabel("ObManage")
        brand.setObjectName("Brand")
        heading.addWidget(brand)
        subtitle = QLabel("仓库镜像，让每一处改动都有一致的副本。")
        subtitle.setObjectName("Subtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        badge = QLabel("单向镜像  /  增量同步")
        badge.setObjectName("Badge")
        header.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        paths = QFrame()
        paths.setObjectName("Panel")
        paths_layout = QVBoxLayout(paths)
        paths_layout.setContentsMargins(18, 14, 18, 14)
        paths_layout.setSpacing(10)
        paths_header = QHBoxLayout()
        paths_title = QLabel("同步位置")
        paths_title.setObjectName("SectionTitle")
        paths_header.addWidget(paths_title)
        paths_header.addStretch()
        direction = QLabel("源仓库  →  目标镜像")
        direction.setObjectName("Muted")
        paths_header.addWidget(direction)
        paths_layout.addLayout(paths_header)

        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        source_label = QLabel("源仓库")
        source_label.setFixedWidth(64)
        source_row.addWidget(source_label)
        self.source_edit = QLineEdit(self.settings.source)
        self.source_edit.setObjectName("source_edit")
        self.source_edit.setPlaceholderText("选择包含完整 Obsidian 仓库的文件夹")
        self.source_edit.setClearButtonEnabled(True)
        source_label.setBuddy(self.source_edit)
        source_row.addWidget(self.source_edit, 1)
        self.source_browse = QPushButton("选择文件夹")
        self.source_browse.setObjectName("source_browse")
        source_row.addWidget(self.source_browse)
        paths_layout.addLayout(source_row)

        target_row = QHBoxLayout()
        target_row.setSpacing(10)
        target_label = QLabel("目标镜像")
        target_label.setFixedWidth(64)
        target_row.addWidget(target_label)
        self.target_combo = DropDownCombo()
        self.target_combo.setObjectName("target_combo")
        self.target_combo.setEditable(True)
        self.target_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.target_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.target_combo.setMinimumContentsLength(10)
        self.target_combo.addItems(list(dict.fromkeys([self.settings.target, *self.settings.recent_targets])))
        self.target_combo.setCurrentText(self.settings.target)
        self.target_combo.lineEdit().setPlaceholderText("所选文件夹就是镜像根目录，不会额外嵌套一层")
        target_label.setBuddy(self.target_combo)
        target_row.addWidget(self.target_combo, 1)
        self.target_browse = QPushButton("选择文件夹")
        self.target_browse.setObjectName("target_browse")
        target_row.addWidget(self.target_browse)
        paths_layout.addLayout(target_row)
        self.path_hint = QLabel("包含隐藏文件和 .obsidian 配置；源端删除的内容，也会从目标移除。")
        self.path_hint.setObjectName("Muted")
        self.path_hint.setWordWrap(True)
        paths_layout.addWidget(self.path_hint)
        layout.addWidget(paths)

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_values: dict[str, QLabel] = {}
        self.stat_details: dict[str, QLabel] = {}
        for action, title, color in (
            ("add", "新增", "#27735B"),
            ("update", "更新", "#326B94"),
            ("delete", "删除", "#AD5748"),
            ("skip", "跳过", "#79848B"),
        ):
            tile = QFrame()
            tile.setObjectName("Stat")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(16, 10, 16, 10)
            tile_layout.setSpacing(3)
            top = QHBoxLayout()
            label = QLabel(title)
            label.setObjectName("StatLabel")
            top.addWidget(label)
            top.addStretch()
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color}; font-size: 9px;")
            top.addWidget(dot)
            tile_layout.addLayout(top)
            value = QLabel("—")
            value.setObjectName("StatValue")
            tile_layout.addWidget(value)
            detail = QLabel("等待分析")
            detail.setObjectName("Muted")
            tile_layout.addWidget(detail)
            self.stat_values[action] = value
            self.stat_details[action] = detail
            stats.addWidget(tile, 1)
        layout.addLayout(stats)

        preview = QFrame()
        preview.setObjectName("Panel")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 8)
        preview_layout.setSpacing(0)
        preview_header = QHBoxLayout()
        preview_header.setContentsMargins(18, 13, 18, 4)
        preview_title = QLabel("差异预览")
        preview_title.setObjectName("SectionTitle")
        preview_header.addWidget(preview_title)
        preview_header.addStretch()
        self.copy_summary = QLabel("待复制  —")
        self.copy_summary.setObjectName("Muted")
        preview_header.addWidget(self.copy_summary)
        preview_layout.addLayout(preview_header)
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(5, 0, 10, 0)
        self.filter_tabs = QTabBar()
        self.filter_tabs.setObjectName("filter_tabs")
        self.filter_tabs.setExpanding(False)
        self.filter_tabs.setDrawBase(False)
        for name, _ in FILTERS:
            self.filter_tabs.addTab(name)
        tabs_row.addWidget(self.filter_tabs)
        tabs_row.addStretch()
        preview_layout.addLayout(tabs_row)
        self.preview_stack = QStackedWidget()
        self.preview_stack.setMinimumHeight(170)
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(20, 18, 20, 18)
        empty_layout.addStretch()
        self.empty_title = QLabel("先看清变化，再开始同步")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setStyleSheet("color: #657984; font-size: 16px; font-weight: 500;")
        self.empty_detail = QLabel("选择同步位置后，点击「分析差异」查看文件变化。")
        self.empty_detail.setObjectName("Muted")
        self.empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_detail.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_detail)
        empty_layout.addStretch()
        self.preview_stack.addWidget(empty_page)
        self.table = QTableView()
        self.table.setObjectName("preview_table")
        self.table_model = PlanTableModel(self)
        self.filter_model = PlanFilterModel(self)
        self.filter_model.setSourceModel(self.table_model)
        self.table.setModel(self.filter_model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(35)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 108)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 250)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_context_menu)
        self.preview_stack.addWidget(self.table)
        preview_layout.addWidget(self.preview_stack, 1)
        layout.addWidget(preview, 1)

        schedule_panel = QFrame()
        schedule_panel.setObjectName("Panel")
        schedule_layout = QHBoxLayout(schedule_panel)
        schedule_layout.setContentsMargins(16, 10, 16, 10)
        schedule_layout.setSpacing(10)
        self.schedule_toggle = QCheckBox("定时同步")
        self.schedule_toggle.setObjectName("schedule_toggle")
        self.schedule_toggle.setChecked(self.settings.schedule_enabled)
        self.schedule_toggle.setToolTip("启用后自动分析并同步当前路径，关闭窗口后继续在托盘运行。")
        schedule_layout.addWidget(self.schedule_toggle)
        self.schedule_mode = DropDownCombo()
        self.schedule_mode.setObjectName("schedule_mode")
        self.schedule_mode.addItem("每隔", "interval")
        self.schedule_mode.addItem("每天", "daily")
        self.schedule_mode.setCurrentIndex(0 if self.settings.schedule_mode == "interval" else 1)
        self.schedule_mode.setFixedWidth(90)
        schedule_layout.addWidget(self.schedule_mode)
        self.interval_spin = QSpinBox()
        self.interval_spin.setObjectName("interval_spin")
        self.interval_spin.setRange(1, 10080)
        self.interval_spin.setValue(self.settings.interval_minutes)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setFixedWidth(136)
        schedule_layout.addWidget(self.interval_spin)
        self.daily_time = QTimeEdit(QTime.fromString(self.settings.daily_time, "HH:mm"))
        self.daily_time.setObjectName("daily_time")
        self.daily_time.setDisplayFormat("HH:mm")
        self.daily_time.setFixedWidth(112)
        schedule_layout.addWidget(self.daily_time)
        schedule_layout.addStretch()
        self.next_run_label = QLabel()
        self.next_run_label.setObjectName("Muted")
        schedule_layout.addWidget(self.next_run_label)
        layout.addWidget(schedule_panel)

        progress_layout = QVBoxLayout()
        progress_layout.setSpacing(6)
        progress_row = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setFixedWidth(12)
        self.status_dot.setStyleSheet("color: #87A38F; font-size: 9px;")
        progress_row.addWidget(self.status_dot)
        self.status_label = QLabel("就绪。首次分析会校验已有副本的内容，以后会更快。")
        self.status_label.setObjectName("status_label")
        self.status_label.setWordWrap(True)
        progress_row.addWidget(self.status_label, 1)
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("Muted")
        progress_row.addWidget(self.speed_label)
        progress_layout.addLayout(progress_row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progress_bar")
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)
        self.current_file = QLabel("未变化文件会跳过复制；完整内容校验可检查隐藏的内容变化。")
        self.current_file.setObjectName("Muted")
        self.current_file.setMinimumWidth(0)
        self.current_file.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        progress_layout.addWidget(self.current_file)
        layout.addLayout(progress_layout)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.logs_button = QPushButton("查看日志")
        self.logs_button.setObjectName("TextButton")
        actions.addWidget(self.logs_button)
        actions.addStretch()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Cancel")
        self.cancel_button.setVisible(False)
        actions.addWidget(self.cancel_button)
        self.deep_button = QPushButton("完整内容校验")
        self.deep_button.setObjectName("deep_button")
        self.deep_button.setToolTip("重新读取并比较全部文件内容，发现大小和修改时间均未变化的内容差异。")
        actions.addWidget(self.deep_button)
        self.analyze_button = QPushButton("分析差异")
        self.analyze_button.setObjectName("analyze_button")
        actions.addWidget(self.analyze_button)
        self.sync_button = QPushButton("开始同步")
        self.sync_button.setObjectName("Primary")
        actions.addWidget(self.sync_button)
        layout.addLayout(actions)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("ObManage · 仓库镜像")
        menu = QMenu(self)
        self.tray_open_action = QAction("打开 ObManage", self)
        self.tray_open_action.triggered.connect(self._show_window)
        menu.addAction(self.tray_open_action)
        self.tray_sync_action = QAction("立即同步…", self)
        self.tray_sync_action.triggered.connect(self._tray_sync)
        menu.addAction(self.tray_sync_action)
        self.tray_pause_action = QAction("暂停定时", self)
        self.tray_pause_action.triggered.connect(self.pause_schedule)
        menu.addAction(self.tray_pause_action)
        menu.addSeparator()
        self.tray_exit_action = QAction("退出", self)
        self.tray_exit_action.triggered.connect(self.request_exit)
        menu.addAction(self.tray_exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._show_window)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _connect_signals(self) -> None:
        self.source_browse.clicked.connect(lambda: self._browse(True))
        self.target_browse.clicked.connect(lambda: self._browse(False))
        self.source_edit.textChanged.connect(self._paths_changed)
        self.target_combo.currentTextChanged.connect(self._paths_changed)
        self.analyze_button.clicked.connect(lambda: self.analyze())
        self.deep_button.clicked.connect(lambda: self.analyze(deep=True))
        self.sync_button.clicked.connect(self.start_sync)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.logs_button.clicked.connect(self.show_logs)
        self.filter_tabs.currentChanged.connect(self._change_filter)
        self.schedule_toggle.toggled.connect(self._schedule_changed)
        self.schedule_mode.currentIndexChanged.connect(self._schedule_changed)
        self.interval_spin.valueChanged.connect(self._schedule_changed)
        self.daily_time.timeChanged.connect(self._schedule_changed)

    def _paths(self) -> tuple[str, str]:
        return self.source_edit.text().strip(), self.target_combo.currentText().strip()

    @staticmethod
    def _normalized_path(path: str) -> str:
        if path.startswith("\\\\?\\UNC\\"):
            path = "\\\\" + path[8:]
        elif path.startswith("\\\\?\\"):
            path = path[4:]
        return os.path.normcase(os.path.abspath(os.path.expanduser(path)))

    def _plan_matches_paths(self) -> bool:
        if self.plan is None:
            return False
        source, target = self._paths()
        return (
            self._normalized_path(source) == self._normalized_path(self.plan.source)
            and self._normalized_path(target) == self._normalized_path(self.plan.target)
        )

    def _browse(self, source: bool) -> None:
        if self.busy:
            return
        current = self.source_edit.text() if source else self.target_combo.currentText()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "选择源仓库" if source else "选择目标镜像根目录",
            current,
        )
        if chosen:
            if source:
                self.source_edit.setText(os.path.normpath(chosen))
            else:
                self.target_combo.setCurrentText(os.path.normpath(chosen))

    @Slot()
    def _paths_changed(self, *_: Any) -> None:
        # Programmatic path changes are subject to the same invalidation as typing.
        self._invalidate_plan()
        was_enabled = self.schedule_toggle.isChecked()
        if was_enabled:
            self.pause_schedule()
        self.path_hint.setText(
            "同步位置已变更，定时已暂停。请重新分析差异；重新开启定时后绑定当前位置。"
            if was_enabled
            else "同步位置已变更，请重新分析差异。所选目标文件夹就是镜像根目录。"
        )
        if not self.busy:
            self._set_status("路径已变更，旧预览已清除。")
        self._refresh_actions()
        self._save_timer.start()

    def _invalidate_plan(self) -> None:
        self.plan = None
        self.table_model.set_items([])
        self.preview_stack.setCurrentIndex(0)
        self.empty_title.setText("先看清变化，再开始同步")
        self.empty_detail.setText("选择同步位置后，点击「分析差异」查看文件变化。")
        self.copy_summary.setText("待复制  —")
        for action in self.stat_values:
            self.stat_values[action].setText("—")
            self.stat_details[action].setText("等待分析")
        for index, (name, _) in enumerate(FILTERS):
            self.filter_tabs.setTabText(index, name)

    def _persist(self, remember_target: bool = False) -> None:
        source, target = self._paths()
        self.settings.source = source
        self.settings.target = target
        self.settings.schedule_enabled = self.schedule_toggle.isChecked()
        self.settings.schedule_mode = self.schedule_mode.currentData()
        self.settings.interval_minutes = self.interval_spin.value()
        self.settings.daily_time = self.daily_time.time().toString("HH:mm")
        if remember_target and target:
            self.settings.recent_targets = list(dict.fromkeys([target, *self.settings.recent_targets]))[:10]
            blocker = QSignalBlocker(self.target_combo)
            self.target_combo.clear()
            self.target_combo.addItems(self.settings.recent_targets)
            self.target_combo.setCurrentText(target)
            del blocker
        try:
            self.store.save(self.settings)
        except (OSError, ValueError) as exc:
            self._log(f"无法保存设置：{exc}")
            self._set_status("无法保存设置，详情见日志。", "warning")

    @Slot()
    def _schedule_changed(self, *_: Any) -> None:
        if self.schedule_toggle.isChecked():
            source, target = self._paths()
            if not source or not target:
                blocker = QSignalBlocker(self.schedule_toggle)
                self.schedule_toggle.setChecked(False)
                del blocker
                self._set_status("请先填写源仓库和目标镜像路径。", "warning")
            else:
                self.settings.bound_source = source
                self.settings.bound_target = target
        self._persist()
        self.scheduler.configure(self.settings)
        self._refresh_schedule_controls()
        self._refresh_actions()
        if self.settings.schedule_enabled:
            self.path_hint.setText("定时已绑定当前源仓库和目标；到点自动同步，包括删除。关闭窗口后继续在托盘运行。")
            self._log(f"已启用定时同步：{self.settings.source} → {self.settings.target}")

    @Slot()
    def pause_schedule(self) -> None:
        was_enabled = self.schedule_toggle.isChecked()
        blocker = QSignalBlocker(self.schedule_toggle)
        self.schedule_toggle.setChecked(False)
        del blocker
        self.settings.schedule_enabled = False
        self.scheduler.pause()
        self._persist()
        self._refresh_schedule_controls()
        self._refresh_actions()
        if was_enabled:
            self._log("定时同步已暂停。")
            self.path_hint.setText("定时已暂停。手动同步前请分析差异，源端删除会同步删除目标。")

    def _refresh_schedule_controls(self) -> None:
        interval = self.schedule_mode.currentData() == "interval"
        self.interval_spin.setVisible(interval)
        self.daily_time.setVisible(not interval)
        enabled = self.schedule_toggle.isChecked()
        next_run = self.scheduler.next_run
        if enabled and next_run:
            self.next_run_label.setText(f"下次同步  {next_run:%m-%d %H:%M}")
        else:
            self.next_run_label.setText("已关闭 · 手动同步")
        self.tray_pause_action.setEnabled(enabled and not self._exit_requested)
        self.tray.setToolTip(
            f"ObManage · 下次同步 {next_run:%H:%M}" if enabled and next_run else "ObManage · 手动同步"
        )

    @Slot()
    def _timer_tick(self) -> None:
        if self._exit_requested:
            return
        was_due = self.scheduler.next_run is not None and datetime.now() >= self.scheduler.next_run
        occupied = self.busy or self._confirming_empty
        due = self.scheduler.tick(occupied)
        if was_due and occupied:
            self._log("到达定时时间，本轮已有任务运行；已跳过，不累计任务。")
        if due:
            if self._paths() != (self.settings.bound_source, self.settings.bound_target):
                self.pause_schedule()
                self._set_status("同步位置已变更，定时已暂停。", "warning")
            else:
                self.analyze(scheduled=True)
        self._refresh_schedule_controls()

    def _refresh_actions(self) -> None:
        available = not self.busy and not self._confirming_empty and not self._exit_requested
        for control in (
            self.source_edit,
            self.source_browse,
            self.target_combo,
            self.target_browse,
            self.schedule_toggle,
            self.schedule_mode,
            self.interval_spin,
            self.daily_time,
        ):
            control.setEnabled(available)
        source, target = self._paths()
        self.analyze_button.setEnabled(available and bool(source and target))
        self.deep_button.setEnabled(available and bool(source and target))
        self.sync_button.setEnabled(
            available
            and self._plan_matches_paths()
            and self.plan.can_execute
            and self.plan.has_changes
        )
        self.cancel_button.setVisible(self.busy)
        self.cancel_button.setEnabled(self.busy and not self._cancel_event.is_set() and not self._exit_requested)
        self.tray_sync_action.setEnabled(available)

    def analyze(self, deep: bool = False, scheduled: bool = False) -> None:
        if self.busy or self._confirming_empty or self._exit_requested:
            return
        source, target = self._paths()
        if not source or not target:
            self._set_status("请先填写源仓库和目标镜像路径。", "warning")
            return
        self._invalidate_plan()
        self.last_result = None
        self._persist(remember_target=True)
        self.empty_title.setText("正在读取仓库变化…")
        self.empty_detail.setText(
            "完整校验会读取两端所有文件内容，大型视频需要一些时间。"
            if deep
            else "已有副本首次需要比较内容；完成的校验记录会保留，可随时取消。"
        )
        self._set_status("正在完整校验内容…" if deep else "正在分析差异…", "busy")
        self._log(f"{'定时' if scheduled else '手动'}{'完整内容校验' if deep else '分析差异'}：{source} → {target}")
        self._start_worker("analyze", deep=deep, scheduled=scheduled)

    @Slot()
    def start_sync(self) -> None:
        if self.busy or self._confirming_empty or self._exit_requested:
            return
        if not self.plan or not self._plan_matches_paths() or not self.plan.can_execute:
            self._set_status("请先重新分析差异，得到可执行的预览。", "warning")
            return
        if not self.plan.has_changes:
            self._set_status("两端已经一致，无需复制。", "success")
            return
        allow_empty = False
        if self._would_empty_target(self.plan):
            # This is the only destructive confirmation; ordinary mirror deletion is
            # already explicitly represented by the preview and the Start action.
            self._confirming_empty = True
            self._refresh_actions()
            try:
                answer = QMessageBox.warning(
                    self,
                    "源仓库为空",
                    f"源仓库目前为空。本次同步将清空下方目标文件夹中的内容：\n\n{self.plan.target}\n\n确认源仓库应当为空，并继续清空目标吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
            finally:
                self._confirming_empty = False
                self._refresh_actions()
            if answer != QMessageBox.StandardButton.Yes:
                self._set_status("已保留目标内容；确认源仓库后可重新分析。")
                return
            if self._exit_requested or not self.plan or not self._plan_matches_paths():
                return
            allow_empty = True
        self._execute_plan(allow_empty=allow_empty, scheduled=False)

    @staticmethod
    def _would_empty_target(plan: SyncPlan) -> bool:
        return plan.source_empty and any(item.action in ("delete", "rmdir") for item in plan.items)

    def _execute_plan(self, allow_empty: bool = False, scheduled: bool = False) -> None:
        self._set_status("正在同步，完成新增与更新后再清理多余内容…", "busy")
        self._log(f"开始同步：预计复制 {format_bytes(self.plan.bytes_to_copy)}。")
        self._start_worker("execute", scheduled=scheduled, allow_empty=allow_empty)

    def _start_worker(
        self,
        operation: str,
        deep: bool = False,
        scheduled: bool = False,
        allow_empty: bool = False,
    ) -> None:
        if self.busy:
            return
        self._cancel_event = threading.Event()
        self._pending_outcome = None
        self._operation = operation
        self._scheduled = scheduled
        self._job_started = time.monotonic()
        self._progress_key = None
        self._copy_started = 0.0
        self.progress_bar.setRange(0, 0)
        self.current_file.setText("正在准备…")
        self.speed_label.setText("")
        source, target = self._paths()
        worker = SyncWorker(
            self.state_dir, operation, source, target, deep,
            self.plan if operation == "execute" else None, allow_empty, self._cancel_event,
        )
        worker.setParent(self)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.finished.connect(self._thread_finished)
        worker.finished.connect(worker.deleteLater)
        self._thread = worker
        self._worker = worker
        self._refresh_actions()
        worker.start()

    @Slot(object)
    def _on_completed(self, outcome: tuple[str, Any]) -> None:
        self._pending_outcome = outcome

    @Slot()
    def _thread_finished(self) -> None:
        # A scheduled execute is started only after the analysis QThread is fully
        # stopped, so callbacks and close requests cannot overlap two operations.
        operation, scheduled = self._operation, self._scheduled
        outcome = self._pending_outcome or ("error", "后台任务结束，但未返回结果。")
        if operation == "analyze" and self._cancel_event.is_set():
            outcome = ("cancelled", None)
        self._worker = None
        self._thread = None
        self._pending_outcome = None
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.speed_label.setText("")
        self._refresh_actions()
        if self._exit_requested:
            self._finish_exit()
            return

        status, payload = outcome
        if status == "cancelled":
            self.plan = None
            self._set_status("已取消。已完成的校验记录可复用；再次同步前请重新分析。", "warning")
            self.current_file.setText("已停止后续操作。")
            self.empty_title.setText("分析已取消")
            self.empty_detail.setText("已完成的内容校验记录已保留，点击「分析差异」即可继续。")
            self._log("任务已取消。")
        elif status == "error":
            self.plan = None
            self._set_status("本轮未完成，请查看日志并重新分析差异。", "error")
            self.current_file.setText(str(payload))
            self.current_file.setToolTip(str(payload))
            self.empty_title.setText("未能完成分析" if operation == "analyze" else "本轮同步未完成")
            self.empty_detail.setText(str(payload))
            self._log(str(payload))
            self._notify("本轮同步未完成", str(payload), warning=True)
        elif operation == "analyze":
            self._accept_plan(payload, scheduled)
        else:
            self._accept_result(payload)
        self._refresh_actions()

    def _accept_plan(self, plan: SyncPlan, scheduled: bool) -> None:
        self.plan = plan
        if not self._plan_matches_paths():
            self._invalidate_plan()
            self._set_status("分析期间路径发生变化，请重新分析差异。", "warning")
            return
        self.table_model.set_items(plan.items)
        self._update_counts(plan)
        self._change_filter(self.filter_tabs.currentIndex())
        self.progress_bar.setValue(1000)
        self.current_file.setText(f"分析完成于 {datetime.now():%H:%M:%S} · 执行前会再次检查路径与文件状态。")
        self.current_file.setToolTip("")
        if not plan.can_execute:
            errors = plan.errors or [item.reason for item in plan.items if item.action == "error"]
            self._set_status(f"本轮无法同步：{errors[0] if errors else '存在无法处理的文件。'}", "error")
            self.empty_title.setText("本轮无法同步")
            self.empty_detail.setText(errors[0] if errors else "请查看异常列表及日志。")
            for error in errors:
                self._log(error)
            if scheduled:
                self._log("定时任务本轮跳过；下次到点重新检查。")
            self._notify("本轮无法同步", errors[0] if errors else "请查看日志。", warning=True)
            return
        counts = plan.counts
        self._log(
            f"分析完成：新增 {counts['add']} 文件 / {counts['mkdir']} 文件夹，"
            f"更新 {counts['update']}，重命名 {counts.get('rename', 0)}，删除 {counts['delete']} 文件 / {counts['rmdir']} 文件夹，"
            f"跳过 {counts['skip']}，待复制 {format_bytes(plan.bytes_to_copy)}。"
        )
        if not plan.has_changes:
            self._set_status("两端已经一致，本轮复制 0 字节。", "success")
            self.empty_title.setText("两端已经一致")
            self.empty_detail.setText("没有需要同步的变化。")
            return
        if scheduled and self._would_empty_target(plan):
            self.pause_schedule()
            self._set_status("源仓库为空，定时已暂停。请打开窗口检查预览并手动确认。", "warning")
            self._log("源仓库为空，本轮未清空目标。需要手动确认后同步。")
            self._notify("需要确认：源仓库为空", "定时已暂停，目标内容已保留。请检查源仓库后手动同步。", warning=True)
            return
        if scheduled:
            # The user enabled automatic mirroring for this exact pair of paths.
            self._execute_plan(scheduled=True)
            return
        self._set_status("预览已就绪。点击「开始同步」应用以上变化，包括删除。", "success")

    def _update_counts(self, plan: SyncPlan) -> None:
        counts = plan.counts
        self.stat_values["add"].setText(f"{counts['add'] + counts['mkdir']:,}")
        self.stat_values["update"].setText(f"{counts['update'] + counts.get('rename', 0):,}")
        self.stat_values["delete"].setText(f"{counts['delete'] + counts['rmdir']:,}")
        self.stat_values["skip"].setText(f"{counts['skip']:,}")
        self.stat_details["add"].setText(f"{counts['add']:,} 文件 · {counts['mkdir']:,} 文件夹")
        self.stat_details["update"].setText(
            f"{counts['update']:,} 内容更新 · {counts['rename']:,} 重命名"
            if counts.get("rename") else "以源仓库的内容覆盖"
        )
        self.stat_details["delete"].setText(f"{counts['delete']:,} 文件 · {counts['rmdir']:,} 文件夹")
        self.stat_details["skip"].setText("内容未变化，无需复制")
        self.copy_summary.setText(f"待复制  {format_bytes(plan.bytes_to_copy)}")
        for index, (name, actions) in enumerate(FILTERS):
            total = len(plan.items) if actions is None else sum(counts.get(action, 0) for action in actions)
            self.filter_tabs.setTabText(index, f"{name} {total:,}")

    def _accept_result(self, result: SyncResult) -> None:
        self.last_result = result
        self.plan = None  # A result never leaves an executable stale preview behind.
        summary = (
            f"复制 {result.copied_files:,} 个文件（{format_bytes(result.copied_bytes)}），"
            f"删除 {result.deleted_files:,} 个文件、{result.deleted_dirs:,} 个文件夹，"
            f"跳过 {result.skipped_files:,} 个文件。"
        )
        if getattr(result, "renamed_items", 0):
            summary += f" 重命名 {result.renamed_items:,} 项。"
        self.copy_summary.setText(f"已复制  {format_bytes(result.copied_bytes)}")
        self.current_file.setText(summary)
        self.current_file.setToolTip(summary)
        self.speed_label.setText(f"用时 {result.duration_seconds:,.1f} 秒")
        if result.status == "success":
            self._set_status("同步完成，目标镜像已更新。", "success")
            self.progress_bar.setValue(1000)
            self._log(f"同步成功：{summary} 用时 {result.duration_seconds:.1f} 秒。")
            if not self.isVisible():
                self._notify("同步完成", summary)
        elif result.status == "cancelled":
            self._set_status("同步已取消，已完成部分保留。下次操作前请重新分析。", "warning")
            self._log(f"同步已取消：{summary}")
        else:
            self._set_status("本轮部分完成，已停止后续操作。请查看日志并重新分析。", "error")
            self._log(f"同步未完成：{summary}")
            self._notify("同步未完成", result.errors[0] if result.errors else "请查看日志。", warning=True)
        for error in result.errors:
            self._log(error)
        self.path_hint.setText("上方为本轮执行前的差异记录。再次同步前请重新分析。")

    @Slot(object)
    def _on_progress(self, progress: Progress) -> None:
        if not self.busy or self._exit_requested:
            return
        if self._cancel_event.is_set():
            return
        names = {
            "scan": "正在扫描文件",
            "hash": "正在校验文件内容",
            "compare": "正在比较差异",
            "copy": "正在复制文件",
            "copied": "正在复制文件",
            "delete": "正在清理目标多余内容",
            "done": "正在完成本轮任务",
        }
        if progress.phase == "hash":
            message = "当前文件写入校验" if self._operation == "execute" else "当前文件内容校验"
        else:
            message = progress.message or names.get(progress.phase, "正在处理…")
        self._set_status(message, "busy")
        path = progress.relative_path or "正在检查仓库…"
        self.current_file.setText(path)
        self.current_file.setToolTip(path)
        if progress.total_bytes > 0:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(progress.completed_bytes * 1000 / progress.total_bytes)))
            now = time.monotonic()
            key = (progress.phase, progress.relative_path)
            if key != self._progress_key or progress.completed_bytes < self._progress_last_bytes:
                self._progress_started = now
                self._progress_initial_bytes = progress.completed_bytes
            self._progress_key = key
            self._progress_last_bytes = progress.completed_bytes
            if progress.phase in ("copy", "copied"):
                if not self._copy_started:
                    self._copy_started = now
                    self._copy_initial_bytes = progress.completed_bytes
                started = self._copy_started
                initial_bytes = self._copy_initial_bytes
            else:
                started = self._progress_started
                initial_bytes = self._progress_initial_bytes
            started = getattr(progress, "_rate_started", started)
            initial_bytes = getattr(progress, "_rate_initial_bytes", initial_bytes)
            elapsed = now - started
            rate = max(0, progress.completed_bytes - initial_bytes) / max(0.1, elapsed)
            speed = f" · {format_bytes(rate)}/秒" if elapsed >= 0.15 else ""
            label = "当前文件 " if progress.phase == "hash" else "总计 "
            self.speed_label.setText(
                f"{label}{format_bytes(progress.completed_bytes)} / {format_bytes(progress.total_bytes)}{speed}"
            )
        elif progress.total_files > 0:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(progress.completed_files * 1000 / progress.total_files)))
            self.speed_label.setText(f"{progress.completed_files:,} / {progress.total_files:,} 项")
        else:
            self.progress_bar.setRange(0, 0)
            self.speed_label.setText(f"{progress.completed_files:,} 项" if progress.completed_files else "")

    @Slot()
    def cancel_operation(self) -> None:
        if not self.busy:
            return
        self._cancel_event.set()
        self._set_status("正在取消，请等待当前操作安全停止…", "warning")
        self.cancel_button.setEnabled(False)
        self._log("已请求取消，等待后台任务停止。")

    def _set_status(self, text: str, kind: str = "neutral") -> None:
        colors = {"neutral": "#8A9A9F", "busy": "#5E8AA2", "success": "#53916F", "warning": "#B38749", "error": "#B66B5D"}
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.status_dot.setStyleSheet(f"color: {colors.get(kind, colors['neutral'])}; font-size: 9px;")

    @Slot(int)
    def _change_filter(self, index: int) -> None:
        self.filter_model.set_actions(FILTERS[max(0, index)][1])
        if self.filter_model.rowCount():
            self.preview_stack.setCurrentIndex(1)
        else:
            self.preview_stack.setCurrentIndex(0)
            if self.table_model.items:
                self.empty_title.setText("此分类没有文件")
                self.empty_detail.setText("切换其他分类查看本轮差异。")

    def _table_context_menu(self, position: Any) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        menu = QMenu(self)
        copy = menu.addAction("复制相对路径")
        if menu.exec(self.table.viewport().mapToGlobal(position)) == copy:
            paths = [self.filter_model.index(index.row(), 1).data() for index in rows]
            QApplication.clipboard().setText("\n".join(paths))

    def _load_log(self) -> None:
        path = self.state_dir / "ui.log"
        try:
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 256_000))
                    self._log_lines = stream.read().decode("utf-8", errors="replace").splitlines()[-3000:]
        except OSError:
            self._log_lines = []

    def _log(self, message: str) -> None:
        logging.getLogger(__name__).info(message)
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-3000:]
        if self._log_dialog is not None:
            self.log_view.appendPlainText(line)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            path = self.state_dir / "ui.log"
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(self.state_dir / "ui.previous.log")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

    @Slot()
    def show_logs(self) -> None:
        if self._log_dialog is None:
            self._log_dialog = QDialog(self)
            self._log_dialog.setWindowTitle("ObManage · 操作日志")
            self._log_dialog.resize(920, 540)
            log_layout = QVBoxLayout(self._log_dialog)
            log_layout.setContentsMargins(20, 20, 20, 20)
            label = QLabel("操作记录与失败原因")
            label.setObjectName("SectionTitle")
            log_layout.addWidget(label)
            self.log_view = QPlainTextEdit()
            self.log_view.setObjectName("log_view")
            self.log_view.setReadOnly(True)
            self.log_view.setMaximumBlockCount(3000)
            self.log_view.setPlainText("\n".join(self._log_lines))
            log_layout.addWidget(self.log_view)
            bottom = QHBoxLayout()
            hint = QLabel(str(self.state_dir / "ui.log"))
            hint.setObjectName("Muted")
            hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bottom.addWidget(hint, 1)
            close = QPushButton("关闭")
            close.clicked.connect(self._log_dialog.hide)
            bottom.addWidget(close)
            log_layout.addLayout(bottom)
        self._log_dialog.show()
        self._log_dialog.raise_()
        self._log_dialog.activateWindow()

    def _notify(self, title: str, message: str, warning: bool = False) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(
                title,
                message[:350],
                QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information,
                8000,
            )

    @Slot()
    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot()
    def _tray_sync(self) -> None:
        self._show_window()
        if not self.busy:
            self.analyze()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    @Slot()
    def request_exit(self) -> None:
        if self._exit_requested:
            return
        self._exit_requested = True
        self._timer.stop()
        self._save_timer.stop()
        self._persist()
        if self.busy:
            self._cancel_event.set()
            self._set_status("正在安全停止后台任务，完成后退出…", "warning")
            self._refresh_actions()
            return
        self._finish_exit()

    def _finish_exit(self) -> None:
        self.tray.hide()
        if self._log_dialog:
            self._log_dialog.hide()
        self.hide()
        QApplication.instance().quit()

    def closeEvent(self, event: Any) -> None:
        if self._exit_requested:
            if self.busy:
                event.ignore()
            else:
                event.accept()
            return
        if self.busy or self.schedule_toggle.isChecked():
            event.ignore()
            self._persist()
            if self.tray.isVisible():
                self.hide()
                if not self._tray_close_notified:
                    self._notify("ObManage 已进入托盘", "后台任务与定时同步将继续运行。需要停止时，请从托盘菜单选择「退出」。")
                    self._tray_close_notified = True
            else:
                self._set_status("系统托盘不可用，窗口保留以便管理后台任务。", "warning")
            return
        event.accept()
        self.request_exit()
