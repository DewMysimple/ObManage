from datetime import datetime, timedelta

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (QApplication, QDateTimeEdit, QDialog, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from ..operation_log import filter_entries
from .registry import FEATURES
from .controls import DropDownCombo


TITLES = {**{f.key: f.short_title for f in FEATURES}, "system": "应用", "legacy": "历史日志"}


class OperationLogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.issues = []
        self.filtered = []
        self.setWindowTitle("ObManage · 操作日志")
        self.resize(980, 640)
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("操作日志")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        first = QHBoxLayout()
        self.period = DropDownCombo()
        for label, value in (("全部保留记录", "all"), ("今天", "today"), ("最近 24 小时", "day"), ("最近 7 天", "week"), ("自选时间段", "custom")):
            self.period.addItem(label, value)
        first.addWidget(self.period)
        self.feature = DropDownCombo()
        self.feature.addItem("全部功能", "")
        for key, title in TITLES.items():
            self.feature.addItem(title, key)
        first.addWidget(self.feature)
        self.event = DropDownCombo()
        self.event.addItem("全部事件", "")
        first.addWidget(self.event)
        self.level = DropDownCombo()
        for label, key in (("全部状态", ""), ("失败", "error"), ("提醒", "warning"), ("成功", "success"), ("取消", "cancelled"), ("记录", "info")):
            self.level.addItem(label, key)
        first.addWidget(self.level)
        first.addStretch()
        reset = QPushButton("重置筛选")
        reset.clicked.connect(self.reset_filters)
        first.addWidget(reset)
        layout.addLayout(first)
        self.time_widget = QWidget()
        timerow = QHBoxLayout(self.time_widget)
        timerow.setContentsMargins(0, 0, 0, 0)
        self.start = QDateTimeEdit(QDateTime.currentDateTime().addDays(-1))
        self.end = QDateTimeEdit(QDateTime.currentDateTime())
        for caption, edit in (("开始", self.start), ("结束", self.end)):
            timerow.addWidget(QLabel(caption))
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            timerow.addWidget(edit)
            edit.dateTimeChanged.connect(self.refresh)
        timerow.addStretch()
        layout.addWidget(self.time_widget)
        second = QHBoxLayout()
        self.task = DropDownCombo()
        self.task.setMinimumWidth(210)
        self.task.setMaximumWidth(380)
        self.task.addItem("全部任务", "")
        second.addWidget(self.task, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索路径、仓库名称或日志内容…")
        self.search.setClearButtonEnabled(True)
        second.addWidget(self.search, 1)
        layout.addLayout(second)
        self.summary = QLabel()
        self.summary.setObjectName("Muted")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setObjectName("log_view")
        self.view.setStyleSheet("QPlainTextEdit { font-size: 13px; padding: 8px; }")
        layout.addWidget(self.view, 1)
        bottom = QHBoxLayout()
        hint = QLabel("按新到旧显示 · 历史记录没有任务标签时可按时间和关键词查找")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        bottom.addWidget(hint, 1)
        copy = QPushButton("复制筛选结果")
        copy.clicked.connect(self.copy_results)
        bottom.addWidget(copy)
        close = QPushButton("关闭")
        close.clicked.connect(self.hide)
        bottom.addWidget(close)
        layout.addLayout(bottom)
        for combo in (self.period, self.feature, self.event, self.level, self.task):
            combo.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        self.refresh()

    def set_entries(self, entries, issues=()):
        self.entries, self.issues = entries, list(issues)
        selected_event, selected_task = self.event.currentData(), self.task.currentData()
        self.event.blockSignals(True)
        self.task.blockSignals(True)
        self.event.clear()
        self.event.addItem("全部事件", "")
        for event in sorted({e.event for e in entries}):
            self.event.addItem(event, event)
        self.task.clear()
        self.task.addItem("全部任务", "")
        seen = set()
        for entry in reversed(entries):
            if entry.task and entry.task not in seen:
                seen.add(entry.task)
                label = f"{entry.timestamp} · {TITLES.get(entry.feature, entry.feature)} · {entry.event} · {entry.task[:6]}"
                self.task.addItem(label, entry.task)
        self.event.setCurrentIndex(max(0, self.event.findData(selected_event)))
        self.task.setCurrentIndex(max(0, self.task.findData(selected_task)))
        self.event.blockSignals(False)
        self.task.blockSignals(False)
        self.refresh()

    def refresh(self, *_):
        period = self.period.currentData()
        self.time_widget.setVisible(period == "custom")
        start = end = None
        now = datetime.now()
        if period == "custom":
            start = self.start.dateTime().toPython().replace(microsecond=0)
            end = self.end.dateTime().toPython().replace(microsecond=0)
        elif period != "all":
            start = (now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "today"
                     else now - timedelta(days=7 if period == "week" else 1))
            end = now
        if start is not None and end is not None and start > end:
            self.filtered = []
            self.summary.setText("开始时间不能晚于结束时间。")
            self.view.clear()
            return
        self.filtered = filter_entries(self.entries, start=start, end=end,
            feature=self.feature.currentData(), event=self.event.currentData(),
            task=self.task.currentData(), level=self.level.currentData(), query=self.search.text())
        display = self.filtered[:2000]
        text = "\n".join(e.display(TITLES.get(e.feature, e.feature)) for e in display)
        self.view.setPlainText(text)
        self.view.verticalScrollBar().setValue(0)
        summary = f"匹配 {len(self.filtered):,} / {len(self.entries):,} 条记录"
        if len(self.filtered) > 2000:
            summary += " · 显示最新 2,000 条；缩小时间段可查看更早记录，复制包含全部匹配项"
        if self.issues:
            summary += f" · {len(self.issues)} 项读取问题（悬停查看）"
        self.summary.setText(summary)
        self.summary.setToolTip("\n".join(self.issues))

    def reset_filters(self):
        for combo in (self.period, self.feature, self.event, self.level, self.task):
            combo.setCurrentIndex(0)
        self.search.clear()
        self.refresh()

    def copy_results(self):
        QApplication.clipboard().setText("\n".join(e.display(TITLES.get(e.feature, e.feature)) for e in self.filtered))
