from __future__ import annotations

import logging
import ntpath
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
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
    QScrollArea,
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
    ("待执行", {"add", "mkdir", "update", "rename", "delete", "rmdir", "error"}),
    ("全部", None),
    ("新增", {"add", "mkdir"}),
    ("更新", {"update", "rename"}),
    ("删除", {"delete", "rmdir"}),
    ("跳过", {"skip"}),
    ("异常", {"error"}),
)


def drive_label(path: str) -> str:
    """Show the configured drive/share, including Windows paths in headless tests."""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    drive = ntpath.splitdrive(path)[0]
    return drive.upper() if len(drive) == 2 and drive[1] == ":" else drive


def endpoint_label(name: str, path: str) -> str:
    drive = drive_label(path)
    return f"{name}（{drive}）" if drive else name


def absolute_item_path(root: str, relative: str) -> str:
    if not root:
        return relative
    if ntpath.splitdrive(root)[0]:
        return ntpath.normpath(ntpath.join(root, relative))
    return os.path.abspath(os.path.join(root, relative))


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

    ACTION_COLUMN, TARGET_COLUMN, SIZE_COLUMN, REASON_COLUMN = range(4)
    HEADERS = ("操作", "实际修改位置", "大小", "说明")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.items: list[PlanItem] = []
        self.source_path = ""
        self.target_path = ""
        self.source_name = "来源仓库"
        self.target_name = "目标仓库"

    def set_context(self, source: str, target: str, source_name: str, target_name: str) -> None:
        self.beginResetModel()
        self.source_path, self.target_path = source, target
        self.source_name, self.target_name = source_name, target_name
        self.endResetModel()

    def action_text(self, item: PlanItem) -> str:
        if item.action in ("delete", "rmdir"):
            return f"仅从{self.target_name}删除"
        if item.action == "skip":
            return "跳过 · 无需修改"
        if item.action == "error":
            return "无法处理"
        verb = {"add": "新增", "mkdir": "新建文件夹", "update": "覆盖更新", "rename": "重命名"}.get(item.action, item.action)
        return f"在{self.target_name}{verb}"

    def reason_text(self, item: PlanItem) -> str:
        if item.action in ("delete", "rmdir"):
            return "来源中已不存在"
        if item.action == "add":
            return "目标缺少此文件，复制来源内容"
        if item.action == "mkdir":
            return "目标缺少此文件夹"
        if item.action == "update":
            return "内容不同，使用来源版本"
        return item.reason.replace("源端", f"{self.source_name}来源").replace("目标端", self.target_name).replace("源仓库", self.source_name)

    def set_items(self, items: list[PlanItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                self.action_text(item),
                absolute_item_path(self.target_path, item.relative_path),
                "—" if item.action in ("mkdir", "rmdir") else format_bytes(item.size),
                self.reason_text(item),
            )[column]
        if role == Qt.ItemDataRole.UserRole:
            return item.action
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"{self.action_text(item)}\n"
                f"实际修改位置：{'无' if item.action in ('skip', 'error') else absolute_item_path(self.target_path, item.relative_path)}\n"
                f"目标位置：{absolute_item_path(self.target_path, item.relative_path)}\n"
                f"对应来源：{absolute_item_path(self.source_path, item.relative_path)}\n"
                f"{self.reason_text(item)}\n"
                f"{item.reason}\n"
                f"{self.source_name}只读取，不覆盖或删除。"
            )
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


class ResponsivePlanTableView(QTableView):
    """Give every viewport pixel to a column, including after scrollbar changes."""

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
            # A filter can add/remove the vertical scrollbar without resizing
            # the table itself. Fit to the viewport after Qt updates its layout.
            QTimer.singleShot(0, self.fit_columns)
        return result

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.fit_columns()

    @Slot()
    def fit_columns(self) -> None:
        if self.model() is None or self.model().columnCount() != len(PlanTableModel.HEADERS):
            return
        width = self.viewport().width()
        action_width, size_width = 210, 88
        flexible = max(80, width - action_width - size_width)
        path_width = round(flexible * 0.68)
        widths = (action_width, path_width, size_width, flexible - path_width)
        for column, column_width in enumerate(widths):
            self.horizontalHeader().resizeSection(column, column_width)


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
QLabel#Brand { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }
QLabel#Subtitle, QLabel#Muted { color: #758189; font-size: 12px; }
QLabel#SectionTitle { font-size: 14px; font-weight: 600; }
QLabel#Direction { font-size: 13px; font-weight: 600; color: #315F7B; }
QLabel#SourceSafety { color: #27735B; font-size: 12px; }
QLabel#TargetEffect { color: #845B35; font-size: 12px; }
QLabel#Badge { background: #E5EBED; color: #48626F; border-radius: 12px; padding: 7px 12px; font-size: 12px; }
QFrame#Panel, QFrame#Stat { background: #FFFFFF; border: 1px solid #DFE5E5; border-radius: 12px; }
QLabel#StatLabel { color: #6A7980; font-size: 12px; }
QLabel#StatValue { font-size: 21px; font-weight: 600; }
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
QPushButton#DirectionChoice { min-height: 30px; font-size: 13px; }
QPushButton#DirectionChoice:checked { background: #EAF2F6; color: #254E68; border: 2px solid #60859B; font-weight: 600; }
QPushButton#DirectionChoice:disabled { color: #8296A0; border-color: #D4DEE3; }
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
QTableView { background: #FFFFFF; border: none; border-top: 1px solid #EBEFEF; selection-background-color: #EAF1F5; selection-color: #23353F; gridline-color: #DEE5E8; outline: none; }
QTableView::item { padding: 0 10px; border-bottom: 1px solid #F0F3F4; border-right: 1px solid #DEE5E8; }
QTableView::item:selected { background: #EAF1F5; color: #23353F; }
QHeaderView::section { background: #F8FAFA; color: #74818A; border: none; border-bottom: 1px solid #DEE5E8; border-right: 1px solid #D3DDE1; padding: 8px 10px; text-align: left; font-size: 12px; font-weight: 400; }
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
        self.resize(1140, 920)
        self.setMinimumSize(980, 620)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._build_tray()
        self._connect_signals()
        self._refresh_direction_text()
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
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.content_scroll.setWidget(content)
        outer.addWidget(self.content_scroll, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 12, 20, 6)
        layout.setSpacing(8)

        header = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(self.windowIcon().pixmap(32, 32))
        mark.setFixedSize(32, 32)
        header.addWidget(mark)
        brand = QLabel("ObManage")
        brand.setObjectName("Brand")
        header.addWidget(brand)
        subtitle = QLabel("本机与移动硬盘之间的增量镜像")
        subtitle.setObjectName("Subtitle")
        header.addSpacing(8)
        header.addWidget(subtitle)
        header.addStretch()
        layout.addLayout(header)

        self.paths_panel = paths = QFrame()
        paths.setObjectName("Panel")
        paths_layout = QVBoxLayout(paths)
        paths_layout.setContentsMargins(14, 10, 14, 10)
        paths_layout.setSpacing(6)

        direction_row = QHBoxLayout()
        direction_row.setSpacing(10)
        paths_title = QLabel("同步方向")
        paths_title.setFixedWidth(90)
        direction_row.addWidget(paths_title)
        self.direction_to_local_button = QPushButton("带回本机  ·  移动硬盘 → 本机")
        self.direction_to_portable_button = QPushButton("带到移动硬盘  ·  本机 → 移动硬盘")
        for button in (self.direction_to_local_button, self.direction_to_portable_button):
            button.setObjectName("DirectionChoice")
            button.setCheckable(True)
            direction_row.addWidget(button, 1)
        self.direction_to_local_button.setToolTip("笔记本工作完成后，以移动硬盘仓库为准，更新当前电脑的仓库。")
        self.direction_to_portable_button.setToolTip("当前电脑工作完成后，以本机仓库为准，更新移动硬盘，随后带到另一台电脑。")
        paths_layout.addLayout(direction_row)

        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        source_label = QLabel("本机仓库")
        source_label.setFixedWidth(90)
        source_row.addWidget(source_label)
        self.local_edit = QLineEdit(self.settings.local_path)
        self.source_edit = self.local_edit
        self.source_edit.setObjectName("local_edit")
        self.source_edit.setPlaceholderText("当前电脑（主机或笔记本）的 Obsidian 仓库")
        self.source_edit.setClearButtonEnabled(True)
        source_label.setBuddy(self.source_edit)
        source_row.addWidget(self.source_edit, 1)
        self.source_browse = QPushButton("选择文件夹")
        self.source_browse.setObjectName("source_browse")
        source_row.addWidget(self.source_browse)
        paths_layout.addLayout(source_row)

        target_row = QHBoxLayout()
        target_row.setSpacing(10)
        target_label = QLabel("移动硬盘仓库")
        target_label.setFixedWidth(90)
        target_row.addWidget(target_label)
        self.portable_combo = DropDownCombo()
        self.target_combo = self.portable_combo
        self.target_combo.setObjectName("portable_combo")
        self.target_combo.setEditable(True)
        self.target_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.target_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.target_combo.setMinimumContentsLength(10)
        self.target_combo.addItems(list(dict.fromkeys([self.settings.portable_path, *self.settings.recent_targets])))
        self.target_combo.setCurrentText(self.settings.portable_path)
        self.target_combo.lineEdit().setPlaceholderText("移动硬盘上的仓库根目录，可选择任意盘符")
        target_label.setBuddy(self.target_combo)
        target_row.addWidget(self.target_combo, 1)
        self.target_browse = QPushButton("选择文件夹")
        self.target_browse.setObjectName("target_browse")
        target_row.addWidget(self.target_browse)
        paths_layout.addLayout(target_row)
        self.direction_label = QLabel()
        self.direction_label.setObjectName("Direction")
        self.source_safety_label = QLabel()
        self.source_safety_label.setObjectName("SourceSafety")
        self.target_effect_label = QLabel()
        self.target_effect_label.setObjectName("TargetEffect")
        safety_row = QHBoxLayout()
        safety_row.setSpacing(16)
        safety_row.addWidget(self.direction_label)
        for label in (self.source_safety_label, self.target_effect_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            safety_row.addWidget(label)
        safety_row.addStretch()
        paths_layout.addLayout(safety_row)
        self.paths_panel.setToolTip("先选方向，再分析差异；所有删除与覆盖只发生在本次接收更新的仓库。")
        layout.addWidget(paths)

        self.stats_panel = QFrame()
        self.stats_panel.setObjectName("Panel")
        stats = QHBoxLayout(self.stats_panel)
        stats.setContentsMargins(0, 0, 0, 0)
        stats.setSpacing(0)
        self.stat_values: dict[str, QLabel] = {}
        self.stat_details: dict[str, QLabel] = {}
        self.stat_titles: dict[str, QLabel] = {}
        for action, title, color in (
            ("add", "新增", "#27735B"),
            ("update", "更新", "#326B94"),
            ("delete", "删除", "#AD5748"),
            ("skip", "跳过", "#79848B"),
        ):
            tile = QFrame()
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(14, 5, 14, 5)
            tile_layout.setSpacing(0)
            top = QHBoxLayout()
            label = QLabel(title)
            label.setObjectName("StatLabel")
            self.stat_titles[action] = label
            top.addWidget(label)
            top.addStretch()
            value = QLabel("—")
            value.setObjectName("StatValue")
            value.setStyleSheet(f"color: {color};")
            top.addWidget(value)
            tile_layout.addLayout(top)
            detail = QLabel("等待分析")
            detail.setObjectName("Muted")
            tile_layout.addWidget(detail)
            self.stat_values[action] = value
            self.stat_details[action] = detail
            stats.addWidget(tile, 1)
            if action != "skip":
                divider = QFrame()
                divider.setFrameShape(QFrame.Shape.VLine)
                divider.setStyleSheet("color: #E1E7E9; background: #E1E7E9;")
                divider.setFixedWidth(1)
                stats.addWidget(divider)
        layout.addWidget(self.stats_panel)

        preview = QFrame()
        preview.setObjectName("Panel")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 8)
        preview_layout.setSpacing(0)
        preview_header = QHBoxLayout()
        preview_header.setContentsMargins(14, 8, 14, 0)
        self.preview_title = QLabel()
        self.preview_title.setObjectName("SectionTitle")
        preview_header.addWidget(self.preview_title)
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
        self.preview_stack.setMinimumHeight(145)
        self.preview_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
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
        self.table = ResponsivePlanTableView()
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
        self.table.fit_columns()
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_context_menu)
        self.preview_stack.addWidget(self.table)
        preview_layout.addWidget(self.preview_stack, 1)
        layout.addWidget(preview, 1)

        schedule_panel = QFrame()
        schedule_panel.setObjectName("Panel")
        schedule_layout = QHBoxLayout(schedule_panel)
        schedule_layout.setContentsMargins(14, 5, 14, 5)
        schedule_layout.setSpacing(10)
        self.schedule_toggle = QCheckBox("定时按此方向同步（含删除）")
        self.schedule_toggle.setObjectName("schedule_toggle")
        self.schedule_toggle.setChecked(self.settings.schedule_enabled)
        self.schedule_toggle.setToolTip("勾选即按上方方向自动更新目标，包括删除。切换方向或仓库会暂停定时，需重新勾选。")
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
        progress_layout.setSpacing(4)
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
        self.current_file = QLabel("")
        self.current_file.setObjectName("Muted")
        self.current_file.setMinimumWidth(0)
        self.current_file.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        progress_layout.addWidget(self.current_file)

        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(20, 6, 20, 10)
        footer_layout.setSpacing(6)
        footer_layout.addLayout(progress_layout)
        self.confirm_direction_checkbox = QCheckBox()
        self.confirm_direction_checkbox.setObjectName("confirm_direction_checkbox")
        self.confirm_direction_checkbox.setEnabled(False)
        footer_layout.addWidget(self.confirm_direction_checkbox)
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
        self.deep_button.setToolTip("重新读取并比较全部文件内容；两端位于不同磁盘时会并行校验。")
        actions.addWidget(self.deep_button)
        self.analyze_button = QPushButton("分析差异")
        self.analyze_button.setObjectName("analyze_button")
        actions.addWidget(self.analyze_button)
        self.sync_button = QPushButton("开始同步")
        self.sync_button.setObjectName("Primary")
        actions.addWidget(self.sync_button)
        footer_layout.addLayout(actions)
        outer.addWidget(footer)

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
        self.direction_to_local_button.clicked.connect(lambda: self.set_direction("to_local"))
        self.direction_to_portable_button.clicked.connect(lambda: self.set_direction("to_portable"))
        self.confirm_direction_checkbox.toggled.connect(self._refresh_actions)
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
        local, portable = self.local_edit.text().strip(), self.portable_combo.currentText().strip()
        return (portable, local) if self.settings.direction == "to_local" else (local, portable)

    def _endpoint_names(self) -> tuple[str, str]:
        local = endpoint_label("本机", self.local_edit.text().strip())
        portable = endpoint_label("移动硬盘", self.portable_combo.currentText().strip())
        return (portable, local) if self.settings.direction == "to_local" else (local, portable)

    def _refresh_direction_text(self) -> None:
        source, target = self._paths()
        source_name, target_name = self._endpoint_names()
        to_local = self.settings.direction == "to_local"
        self.direction_to_local_button.setChecked(to_local)
        self.direction_to_portable_button.setChecked(not to_local)
        self.direction_label.setText(f"{source_name}  →  {target_name}")
        self.direction_label.setToolTip(f"本次以来源为准：{source}\n仅修改目标：{target}")
        self.source_safety_label.setText("来源只读取")
        self.source_safety_label.setToolTip(f"{source_name}只读取，不覆盖或删除。\n{source}")
        self.target_effect_label.setText("仅目标新增、覆盖与删除")
        self.target_effect_label.setToolTip(f"新增、覆盖与删除仅发生在{target_name}。\n{target}")
        self.preview_title.setText(f"差异预览 · 仅修改{target_name}")
        self.preview_title.setToolTip(f"本次所有操作的目标根目录：{target}\n来源只读取：{source}")
        for action, verb in (("add", "新增"), ("update", "更新"), ("delete", "删除"), ("skip", "跳过")):
            self.stat_titles[action].setText(verb)
            self.stat_titles[action].setToolTip(f"本次目标：{target_name}\n{target}")
        self.confirm_direction_checkbox.setText(
            f"我已确认：以{source_name}为准，仅更新{target_name}，包括预览中的删除"
        )
        self.confirm_direction_checkbox.setToolTip(
            f"只读取：{source}\n仅修改：{target}\n每次重新分析或切换方向后，需要重新确认本次预览。"
        )
        self.sync_button.setText(f"更新{target_name}")
        self.sync_button.setToolTip(f"仅修改 {target}；{source} 只读取，不覆盖或删除。")
        self.table_model.set_context(source, target, source_name, target_name)
        if hasattr(self, "tray_sync_action"):
            self.tray_sync_action.setText(f"分析{source_name} → {target_name}…")

    def set_direction(self, direction: str) -> None:
        if direction not in ("to_local", "to_portable"):
            raise ValueError("同步方向无效")
        if self.busy or self._confirming_empty or self._exit_requested:
            self._refresh_direction_text()
            return
        if direction == self.settings.direction:
            self._refresh_direction_text()
            return
        self.settings.set_endpoints(self.local_edit.text().strip(), self.portable_combo.currentText().strip())
        self.settings.set_direction(direction)
        self._invalidate_plan()
        self.pause_schedule()
        self._refresh_direction_text()
        source_name, target_name = self._endpoint_names()
        self.paths_panel.setToolTip("方向已切换，旧预览与确认已清除，定时已暂停。请重新分析差异。")
        self.current_file.setText(f"{source_name}只读取；新增、更新与删除仅发生在{target_name}。")
        self._set_status(f"已切换为 {source_name} → {target_name}，请重新分析。")
        self._log(f"已切换方向：{source_name} {self._paths()[0]} → {target_name} {self._paths()[1]}；定时已暂停。")
        self._refresh_actions()
        self._persist()

    @staticmethod
    def _normalized_path(path: str) -> str:
        if path.startswith("\\\\?\\UNC\\"):
            path = "\\\\" + path[8:]
        elif path.startswith("\\\\?\\"):
            path = path[4:]
        return os.path.normcase(os.path.realpath(os.path.expanduser(path)))

    def _plan_matches_paths(self) -> bool:
        if self.plan is None:
            return False
        source, target = self._paths()
        return (
            self._normalized_path(source) == self._normalized_path(self.plan.source)
            and self._normalized_path(target) == self._normalized_path(self.plan.target)
        )

    def _browse(self, source: bool) -> None:
        if self.busy or self._confirming_empty or self._exit_requested:
            return
        current = self.source_edit.text() if source else self.target_combo.currentText()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "选择本机仓库（主机或笔记本）" if source else "选择移动硬盘上的仓库根目录",
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
        self.settings.set_endpoints(self.local_edit.text().strip(), self.portable_combo.currentText().strip())
        self.pause_schedule()
        self._refresh_direction_text()
        self.paths_panel.setToolTip(
            "同步位置已变更，定时已暂停。请重新分析差异；重新开启定时后绑定当前位置。"
            if was_enabled
            else "仓库位置已变更，请重新分析差异。上方方向决定本次以哪一端为准。"
        )
        if not self.busy:
            self._set_status("路径已变更，旧预览已清除。")
        self._refresh_actions()
        self._save_timer.start()

    def _invalidate_plan(self) -> None:
        self.plan = None
        self.confirm_direction_checkbox.setChecked(False)
        self.table_model.set_items([])
        self.preview_stack.setCurrentIndex(0)
        self.empty_title.setText("先看清变化，再开始同步")
        self.empty_detail.setText("确认上方方向后，点击「分析差异」。分析只读取两个仓库，不修改文件。")
        self.copy_summary.setText("待复制  —")
        for action in self.stat_values:
            self.stat_values[action].setText("—")
            self.stat_details[action].setText("等待分析")
        for index, (name, _) in enumerate(FILTERS):
            self.filter_tabs.setTabText(index, name)
        self.filter_tabs.setCurrentIndex(0)
        self._change_filter(0)

        self._refresh_direction_text()

    def _persist(self, remember_target: bool = False) -> None:
        source, target = self._paths()
        self.settings.set_endpoints(self.local_edit.text().strip(), self.portable_combo.currentText().strip())
        self.settings.schedule_enabled = self.schedule_toggle.isChecked()
        self.settings.schedule_mode = self.schedule_mode.currentData()
        self.settings.interval_minutes = self.interval_spin.value()
        self.settings.daily_time = self.daily_time.time().toString("HH:mm")
        if remember_target and target:
            portable = self.portable_combo.currentText().strip()
            self.settings.recent_targets = list(dict.fromkeys([portable, *self.settings.recent_targets]))[:10]
            blocker = QSignalBlocker(self.target_combo)
            self.target_combo.clear()
            self.target_combo.addItems(self.settings.recent_targets)
            self.target_combo.setCurrentText(portable)
            del blocker
        try:
            self.store.save(self.settings)
        except (OSError, ValueError) as exc:
            self._log(f"无法保存设置：{exc}")
            self._set_status("无法保存设置，详情见日志。", "warning")

    @Slot()
    def _schedule_changed(self, *_: Any) -> None:
        self.settings.set_endpoints(self.local_edit.text().strip(), self.portable_combo.currentText().strip())
        if self.schedule_toggle.isChecked():
            source, target = self._paths()
            if not source or not target:
                blocker = QSignalBlocker(self.schedule_toggle)
                self.schedule_toggle.setChecked(False)
                del blocker
                self._set_status("请先填写本机仓库和移动硬盘仓库的位置。", "warning")
            else:
                self.settings.bound_source = source
                self.settings.bound_target = target
        self._persist()
        self.scheduler.configure(self.settings)
        self._refresh_schedule_controls()
        self._refresh_actions()
        if self.settings.schedule_enabled:
            source_name, target_name = self._endpoint_names()
            self.paths_panel.setToolTip(f"定时已绑定 {source_name} → {target_name}，仅自动更新{target_name}（含删除）；切换方向后自动暂停。")
            self._log(f"已启用定时同步：只读取 {source_name} {self.settings.source} → 仅更新 {target_name} {self.settings.target}（含删除）")

    @Slot()
    def pause_schedule(self) -> None:
        was_enabled = self.schedule_toggle.isChecked()
        blocker = QSignalBlocker(self.schedule_toggle)
        self.schedule_toggle.setChecked(False)
        del blocker
        self.settings.schedule_enabled = False
        self.settings.bound_source = ""
        self.settings.bound_target = ""
        self.scheduler.pause()
        self._persist()
        self._refresh_schedule_controls()
        self._refresh_actions()
        if was_enabled:
            self._log("定时同步已暂停。")
            self.paths_panel.setToolTip("定时已暂停。请按上方方向分析差异，所有修改与删除仅发生在接收更新的仓库。")

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
            self.direction_to_local_button,
            self.direction_to_portable_button,
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
            and self.confirm_direction_checkbox.isChecked()
        )
        self.confirm_direction_checkbox.setEnabled(
            available and self._plan_matches_paths() and self.plan.can_execute and self.plan.has_changes
        )
        self.cancel_button.setVisible(self.busy)
        self.cancel_button.setEnabled(self.busy and not self._cancel_event.is_set() and not self._exit_requested)
        self.tray_sync_action.setEnabled(available)

    def analyze(self, deep: bool = False, scheduled: bool = False) -> None:
        if self.busy or self._confirming_empty or self._exit_requested:
            return
        source, target = self._paths()
        if not source or not target:
            self._set_status("请先填写本机仓库和移动硬盘仓库的位置。", "warning")
            return
        self._invalidate_plan()
        self.last_result = None
        self._persist(remember_target=True)
        self.empty_title.setText("正在读取仓库变化…")
        self.empty_detail.setText(
            "完整校验仍会读取两端全部内容；不同磁盘会并行进行，速度取决于较慢一端。"
            if deep
            else "已有副本首次需要比较内容；完成的校验记录会保留，可随时取消。"
        )
        source_name, target_name = self._endpoint_names()
        self._set_status(f"正在{'完整校验内容' if deep else '分析差异'}：{source_name} → {target_name}（分析不修改文件）", "busy")
        self._log(f"{'定时' if scheduled else '手动'}{'完整内容校验' if deep else '分析差异'}：以 {source_name} {source} 为准 → 仅修改 {target_name} {target}；分析阶段不修改仓库。")
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
        if not self.confirm_direction_checkbox.isChecked():
            self._set_status("请先核对上方方向与预览，再勾选本次方向确认。", "warning")
            return
        allow_empty = False
        if self._would_empty_target(self.plan):
            # An empty source is additional to the ordinary per-preview direction
            # acknowledgement because this would empty the entire destination.
            source_name, target_name = self._endpoint_names()
            self._confirming_empty = True
            self._refresh_actions()
            try:
                answer = QMessageBox.warning(
                    self,
                    f"{source_name}来源仓库为空",
                    f"本次以 {source_name} 为准，但它目前为空：\n{self.plan.source}\n\n继续将清空 {target_name} 中的内容：\n{self.plan.target}\n\n{source_name}只读取，不覆盖或删除。确认清空 {target_name} 吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
            finally:
                self._confirming_empty = False
                self._refresh_actions()
            if answer != QMessageBox.StandardButton.Yes:
                self._set_status(f"已保留{target_name}内容；确认{source_name}来源仓库后可重新分析。")
                return
            if self._exit_requested or not self.plan or not self._plan_matches_paths():
                return
            allow_empty = True
        self._execute_plan(allow_empty=allow_empty, scheduled=False)

    @staticmethod
    def _would_empty_target(plan: SyncPlan) -> bool:
        return plan.source_empty and any(item.action in ("delete", "rmdir") for item in plan.items)

    def _execute_plan(self, allow_empty: bool = False, scheduled: bool = False) -> None:
        source_name, target_name = self._endpoint_names()
        self._set_status(f"正在更新{target_name}，完成新增与更新后再删除此处多余内容；{source_name}只读取。", "busy")
        self._log(
            f"开始同步：以 {source_name} {self.plan.source} 为准，仅更新 {target_name} {self.plan.target}，"
            f"包括删除；{source_name}不覆盖或删除。预计复制 {format_bytes(self.plan.bytes_to_copy)}。"
        )
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
        self.confirm_direction_checkbox.setChecked(False)
        if not self._plan_matches_paths():
            self._invalidate_plan()
            self._set_status("分析期间路径发生变化，请重新分析差异。", "warning")
            return
        source_name, target_name = self._endpoint_names()
        self.table_model.set_context(plan.source, plan.target, source_name, target_name)
        self.table_model.set_items(plan.items)
        self._update_counts(plan)
        self.filter_tabs.setCurrentIndex(0)
        self._change_filter(0)
        self.progress_bar.setValue(1000)
        self.current_file.setText(f"分析完成于 {datetime.now():%H:%M:%S} · 尚未修改仓库；预览中的删除仅发生在{target_name}。")
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
            f"分析完成：以下操作仅发生在 {target_name} {plan.target}；新增 {counts['add']} 文件 / {counts['mkdir']} 文件夹，"
            f"更新 {counts['update']}，重命名 {counts.get('rename', 0)}，删除 {counts['delete']} 文件 / {counts['rmdir']} 文件夹，"
            f"跳过 {counts['skip']}，待复制 {format_bytes(plan.bytes_to_copy)}。"
        )
        if not plan.has_changes:
            self._set_status(f"{target_name}与{source_name}已经一致，本轮复制 0 字节。", "success")
            self.empty_title.setText("两端已经一致")
            self.empty_detail.setText("没有需要同步的变化。")
            return
        if scheduled and self._would_empty_target(plan):
            self.pause_schedule()
            self._set_status(f"{source_name}来源仓库为空，定时已暂停。请检查预览并手动确认。", "warning")
            self._log(f"{source_name}来源仓库为空，本轮未清空{target_name}。需要手动确认后同步。")
            self._notify(f"需要确认：{source_name}来源仓库为空", f"定时已暂停，{target_name}内容已保留。请检查来源仓库后手动同步。", warning=True)
            return
        if scheduled:
            # The user enabled automatic mirroring for this exact pair of paths.
            self._execute_plan(scheduled=True)
            return
        self._set_status(f"预览已就绪。核对并勾选方向确认后，可更新{target_name}；{source_name}只读取。", "success")
        self._refresh_actions()

    def _update_counts(self, plan: SyncPlan) -> None:
        counts = plan.counts
        source_name, target_name = self._endpoint_names()
        self.stat_values["add"].setText(f"{counts['add'] + counts['mkdir']:,}")
        self.stat_values["update"].setText(f"{counts['update'] + counts.get('rename', 0):,}")
        self.stat_values["delete"].setText(f"{counts['delete'] + counts['rmdir']:,}")
        self.stat_values["skip"].setText(f"{counts['skip']:,}")
        self.stat_details["add"].setText(f"{counts['add']:,} 文件 · {counts['mkdir']:,} 文件夹")
        self.stat_details["update"].setText(
            f"{counts['update']:,} 内容更新 · {counts['rename']:,} 重命名"
            if counts.get("rename") else f"使用{source_name}的内容"
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
        self.confirm_direction_checkbox.setChecked(False)
        source_name, target_name = self._endpoint_names()
        summary = (
            f"{target_name}：复制 {result.copied_files:,} 个文件（{format_bytes(result.copied_bytes)}），"
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
            self._set_status(f"同步完成，{target_name}已更新；本程序未覆盖或删除{source_name}的文件。", "success")
            self.progress_bar.setValue(1000)
            self._log(f"同步成功：{summary} {source_name}只读取，未覆盖或删除；用时 {result.duration_seconds:.1f} 秒。")
            if not self.isVisible():
                self._notify(f"{target_name}更新完成", f"{summary} {source_name}只读取，未覆盖或删除。")
        elif result.status == "cancelled":
            self._set_status(f"已取消更新{target_name}，已完成部分保留；{source_name}只读取。请重新分析。", "warning")
            self._log(f"同步已取消：{summary}")
        else:
            self._set_status(f"{target_name}本轮部分完成，已停止后续操作；{source_name}只读取。请查看日志并重新分析。", "error")
            self._log(f"同步未完成：{summary}")
            self._notify(f"{target_name}更新未完成", result.errors[0] if result.errors else "请查看日志。", warning=True)
        for error in result.errors:
            self._log(error)
        self.preview_title.setText(f"本轮执行前记录 · 操作位置：{target_name}")
        self.paths_panel.setToolTip(f"上方为本轮执行前的记录，操作仅针对{target_name}。再次同步前请重新分析。")

    @Slot(object)
    def _on_progress(self, progress: Progress) -> None:
        if not self.busy or self._exit_requested:
            return
        if self._cancel_event.is_set():
            return
        source_name, target_name = self._endpoint_names()
        names = {
            "scan": "正在扫描文件",
            "hash": "正在校验文件内容",
            "compare": "正在比较差异",
            "copy": "正在复制文件",
            "copied": "正在复制文件",
            "delete": f"正在清理{target_name}多余内容",
            "done": "正在完成本轮任务",
        }
        if progress.phase == "hash":
            message = "当前文件写入校验" if self._operation == "execute" else "当前文件内容校验"
        elif progress.phase == "delete":
            message = f"仅从{target_name}删除多余内容；{source_name}只读取"
        elif progress.phase in ("copy", "copied", "rename"):
            message = f"正在更新{target_name}；{source_name}只读取"
        else:
            message = (progress.message or names.get(progress.phase, "正在处理…")).replace("源端", source_name).replace("目标", target_name)
        self._set_status(message, "busy")
        path = progress.relative_path or "正在检查仓库…"
        actual = absolute_item_path(self._paths()[1], progress.relative_path)
        self.current_file.setText(f"{target_name}：{actual}" if self._operation == "execute" and progress.relative_path else path)
        self.current_file.setToolTip(
            f"本次只读取：{self._paths()[0]}\n本次仅修改：{self._paths()[1]}\n{path}"
        )
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
        copy_target = menu.addAction("复制实际修改位置")
        copy = menu.addAction("复制相对路径")
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen == copy:
            paths = [self.table_model.items[self.filter_model.mapToSource(index).row()].relative_path for index in rows]
            QApplication.clipboard().setText("\n".join(paths))
        elif chosen == copy_target:
            self.copy_selected_target_paths()

    def copy_selected_target_paths(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        paths = [self.filter_model.index(index.row(), PlanTableModel.TARGET_COLUMN).data() for index in rows]
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
