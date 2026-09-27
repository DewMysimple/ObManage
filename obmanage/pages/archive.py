from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QTableView, QVBoxLayout, QWidget)

from ..management.archive import ArchiveEngine, ArchivePlan
from ..management.desktop_actions import open_baidu
from .common import FeaturePage, PathPicker, format_bytes, panel
from .icons import action_icon
from .controls import DropDownCombo, LevelSpinBox


class ArchiveTableModel(QAbstractTableModel):
    headers = ("处理", "包内路径", "原始大小")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plan = None

    def set_plan(self, plan):
        self.beginResetModel()
        self.plan = plan
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.plan is None else len(self.plan.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.plan is None:
            return None
        entry = self.plan.entries[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(Path(self.plan.source) / entry.path)
        if role == Qt.ItemDataRole.DisplayRole:
            return (("排除 · " + entry.excluded) if entry.excluded else
                    "保留目录" if entry.state["kind"] == "dir" else "打包文件",
                    entry.path, format_bytes(entry.state["size"]) if entry.state["kind"] == "file" else "—")[index.column()]


def location_row(layout, title, *, executable=False):
    row = QHBoxLayout()
    caption = QLabel(title)
    caption.setFixedWidth(106)
    edit = QLineEdit()
    edit.setClearButtonEnabled(True)
    edit.textChanged.connect(edit.setToolTip)
    caption.setBuddy(edit)
    button = QPushButton("浏览…")
    button.setIcon(action_icon("folder"))
    button.setProperty("pathAction", True)

    def browse():
        chosen = (QFileDialog.getOpenFileName(edit, "选择百度网盘客户端", edit.text(), "应用程序 (*.exe)")[0]
                  if executable else QFileDialog.getExistingDirectory(edit, title, edit.text()))
        if chosen:
            edit.setText(os.path.normpath(chosen))
    button.clicked.connect(browse)
    row.addWidget(caption)
    row.addWidget(edit, 1)
    row.addWidget(button)
    layout.addLayout(row)
    return edit, button


class ArchivePage(FeaturePage):
    page_key = "archive"

    def __init__(self, state_dir, settings, default_root):
        super().__init__(".Archive", "将仓库或仓库集合打包为 ZIP，保留目录结构和空文件夹，完成后校验内容。")
        self.engine = ArchiveEngine(state_dir)
        self.plan = None
        self.result = None
        self._restoring = True
        self._locations = {"custom": str(settings.get("output_dir", "")), "baidu": str(settings.get("baidu_dir", ""))}
        self._location_mode = "custom"
        _, source = panel(self.body)
        self.source_picker = PathPicker("来源仓库", str(settings.get("source", default_root)))
        source.addWidget(self.source_picker)
        _, options = panel(self.body)
        destination = QHBoxLayout()
        label = QLabel("保存方式")
        label.setFixedWidth(106)
        destination.addWidget(label)
        self.destination_mode = DropDownCombo()
        self.destination_mode.addItem("自选文件夹", "custom")
        self.destination_mode.addItem("百度同步文件夹", "baidu")
        destination.addWidget(self.destination_mode)
        self.remember = QCheckBox("记住保存位置")
        self.remember.setChecked(settings.get("remember", True) is True)
        destination.addWidget(self.remember)
        destination.addStretch()
        options.addLayout(destination)
        self.output_edit, self.output_browse = location_row(options, "保存文件夹")
        name_row = QHBoxLayout()
        caption = QLabel("压缩包名称")
        caption.setFixedWidth(106)
        name_row.addWidget(caption)
        self.filename = QLineEdit()
        self.filename.setPlaceholderText("留空自动生成：仓库名_日期时间.zip")
        name_row.addWidget(self.filename, 1)
        options.addLayout(name_row)
        tuning = QHBoxLayout()
        self.exclude_videos = QCheckBox("排除视频")
        self.exclude_videos.setChecked(settings.get("exclude_videos", False) is True)
        self.exclude_videos.setToolTip("按与仓库备份相同的视频扩展名规则排除；.ts 代码文件保留。")
        tuning.addWidget(self.exclude_videos)
        tuning.addStretch()
        tuning.addWidget(QLabel("压缩方案"))
        self.preset = DropDownCombo()
        for title, value in (("仅打包", 0), ("速度优先", 1), ("均衡", 6), ("体积优先", 9), ("自定义", -1)):
            self.preset.addItem(title, value)
        tuning.addWidget(self.preset)
        tuning.addWidget(QLabel("级别"))
        self.level = LevelSpinBox()
        self.level.setRange(0, 9)
        self.level.setToolTip("0 不压缩；1 至 9，级别越高通常越小、越慢。")
        value = settings.get("level", 1)
        self.level.setValue(value if type(value) is int and 0 <= value <= 9 else 1)
        tuning.addWidget(self.level)
        options.addLayout(tuning)
        self.launch_baidu = QCheckBox("完成后打开百度网盘")
        self.launch_baidu.setChecked(settings.get("launch_baidu", False) is True)
        options.addWidget(self.launch_baidu)
        self.client_widget = QWidget()
        client_layout = QVBoxLayout(self.client_widget)
        client_layout.setContentsMargins(0, 0, 0, 0)
        self.client_edit, self.client_browse = location_row(client_layout, "网盘客户端", executable=True)
        self.client_edit.setPlaceholderText("选择 BaiduNetdisk.exe；只在打包成功后打开")
        self.client_edit.setText(str(settings.get("baidu_exe", "")))
        options.addWidget(self.client_widget)
        self.destination_hint = QLabel("保存位置必须在来源之外。ZIP 内直接保留来源目录内容，不额外套一层目录。")
        self.destination_hint.setWordWrap(True)
        self.destination_hint.setObjectName("Muted")
        options.addWidget(self.destination_hint)
        _, preview = panel(self.body)
        self.summary = QLabel("选择来源和保存位置，再预览打包清单。")
        self.summary.setWordWrap(True)
        preview.addWidget(self.summary)
        self.table = QTableView()
        self.model = ArchiveTableModel(self)
        self.table.setModel(self.model)
        self.table.setMinimumHeight(150)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        preview.addWidget(self.table, 1)
        self.output_label = QLabel("")
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview.addWidget(self.output_label)
        actions = QHBoxLayout()
        self.open_output = QPushButton("打开保存位置")
        self.open_output.clicked.connect(self._open_output)
        actions.addWidget(self.open_output)
        actions.addStretch()
        self.preview_button = QPushButton("预览打包")
        self.preview_button.setIcon(action_icon("search"))
        self.preview_button.clicked.connect(self.analyze)
        actions.addWidget(self.preview_button)
        self.execute_button = QPushButton("开始打包")
        self.execute_button.setObjectName("Primary")
        self.execute_button.clicked.connect(self.execute)
        actions.addWidget(self.execute_button)
        self.body.addLayout(actions)
        self.destination_mode.currentIndexChanged.connect(self._mode_changed)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.level.valueChanged.connect(self._level_changed)
        self.source_picker.changed.connect(self._invalidate)
        for edit in (self.output_edit, self.filename, self.client_edit):
            edit.textChanged.connect(self._invalidate)
        for toggle in (self.exclude_videos, self.remember, self.launch_baidu):
            toggle.toggled.connect(self._invalidate)
        self.destination_mode.setCurrentIndex(1 if settings.get("destination_mode") == "baidu" else 0)
        self._mode_changed()
        self._level_changed()
        self._restoring = False
        self.refresh_actions()

    def _mode_changed(self, *_):
        if not self._restoring:
            self._locations[self._location_mode] = self.output_edit.text().strip()
        self._location_mode = self.destination_mode.currentData()
        self.output_edit.setText(self._locations[self._location_mode])
        self._invalidate()

    def _preset_changed(self, *_):
        value = self.preset.currentData()
        if value is not None and value >= 0:
            self.level.setValue(value)

    def _level_changed(self, *_):
        index = self.preset.findData(self.level.value())
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(index if index >= 0 else self.preset.findData(-1))
        self.preset.blockSignals(False)
        self._invalidate()

    def settings_payload(self):
        self._locations[self._location_mode] = self.output_edit.text().strip()
        return {"source": self.source_picker.value, "remember": self.remember.isChecked(),
                "output_dir": self._locations["custom"] if self.remember.isChecked() else "",
                "baidu_dir": self._locations["baidu"] if self.remember.isChecked() else "",
                "destination_mode": self._location_mode, "level": self.level.value(),
                "exclude_videos": self.exclude_videos.isChecked(),
                "launch_baidu": self.launch_baidu.isChecked(), "baidu_exe": self.client_edit.text().strip()}

    def repository_paths(self):
        return (self.source_picker.value,)

    def _invalidate(self, *_):
        if self._restoring:
            return
        self.plan = None
        self.model.set_plan(None)
        self.summary.setText("设置已改变，请重新预览打包清单。")
        self.output_label.clear()
        self.settings_changed.emit()
        self.refresh_actions()

    def analyze(self):
        if self._global_busy:
            return
        source, directory = self.source_picker.value, self.output_edit.text().strip()
        filename, level, exclude = self.filename.text().strip(), self.level.value(), self.exclude_videos.isChecked()
        self.plan = None
        self.model.set_plan(None)
        self.start_task("preview", lambda cancel, progress: self.engine.analyze(
            source, directory, filename=filename, level=level, exclude_videos=exclude, cancel=cancel, progress=progress))

    def execute(self):
        if self._global_busy or self._external_recovery_pending or self.plan is None:
            return
        plan = self.plan
        self._launch_requested = self._location_mode == "baidu" and self.launch_baidu.isChecked()
        self._client_path = self.client_edit.text().strip()
        if self._launch_requested and not self._client_path:
            self.set_status("请先选择百度网盘客户端，或取消完成后打开。", "warning")
            return
        self.start_task("archive", lambda cancel, progress: self.engine.execute(plan, cancel=cancel, progress=progress))

    def task_finished(self, status, payload):
        super().task_finished(status, payload)
        if status == "ok" and isinstance(payload, ArchivePlan):
            self.plan = payload
            self.model.set_plan(payload)
            excluded = [e for e in payload.entries if e.excluded]
            self.summary.setText(f"打包 {len(payload.files):,} 个文件 · {format_bytes(payload.total_bytes)} · 排除 {len(excluded):,} 项")
            self.output_label.setText("输出：" + payload.output)
            self.output_label.setToolTip(payload.output)
            self.set_status("预览完成，核对清单与输出位置后开始打包。", "success")
            self.message_logged.emit(f"打包预览完成：{payload.source} → {payload.output}；{len(payload.files)} 个文件，排除 {len(excluded)} 项。")
        elif status == "ok":
            self.result = payload
            self.plan = None
            self.set_status(f"打包完成，校验通过 · {format_bytes(payload.archive_bytes)} · {payload.duration:.1f} 秒", "success")
            self.output_label.setText("已保存：" + payload.output)
            self.message_logged.emit(f"打包完成并校验通过：{payload.output}；{payload.files} 个文件，{format_bytes(payload.archive_bytes)}。")
            if self._launch_requested:
                try:
                    message = open_baidu(self._client_path)
                    self.message_logged.emit(message)
                except (OSError, ValueError) as exc:
                    self.set_status(f"压缩包已保存；百度网盘未能打开：{exc}", "warning")
                    self.message_logged.emit(f"压缩包已保存；百度网盘打开失败：{exc}")
        else:
            self.plan = None
        self.refresh_actions()

    def _open_output(self):
        if self.result:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.result.output).parent)))

    def refresh_actions(self):
        enabled = not self._global_busy and not self._task_active
        self.source_picker.set_controls_enabled(enabled)
        for widget in (self.output_edit, self.output_browse, self.filename, self.destination_mode,
                       self.exclude_videos, self.remember, self.preset, self.level,
                       self.launch_baidu, self.client_edit, self.client_browse):
            widget.setEnabled(enabled)
        cloud = self._location_mode == "baidu"
        self.launch_baidu.setVisible(cloud)
        self.client_widget.setVisible(cloud and self.launch_baidu.isChecked())
        self.preview_button.setEnabled(enabled and bool(self.source_picker.value and self.output_edit.text().strip()))
        self.execute_button.setEnabled(enabled and self.plan is not None and not self._external_recovery_pending)
        self.open_output.setEnabled(self.result is not None)
