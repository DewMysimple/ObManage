from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models import Progress
from .icons import action_icon
from .vault_selection import VaultSelection


class PathPicker(QWidget):
    """Three shared vault-selection actions and a full-width path on small windows."""

    changed = Signal(str)

    def __init__(self, label: str, value: str = "", *, dialog_title: str = "选择文件夹") -> None:
        super().__init__()
        self.dialog_title = dialog_title
        outer = QVBoxLayout(self)
        self.outer_layout = outer
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        layout = QHBoxLayout()
        self.input_layout = layout
        outer.addLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        caption = QLabel(label)
        caption.setFixedWidth(106)
        layout.addWidget(caption)
        self.edit = QLineEdit(value)
        self.edit.setClearButtonEnabled(True)
        caption.setBuddy(self.edit)
        layout.addWidget(self.edit, 1)
        self.browse = QPushButton("浏览…")
        self.browse.clicked.connect(self._browse)
        self.browse.setIcon(action_icon("folder"))
        self.vault_list = QPushButton("仓库列表")
        self.vault_list.setToolTip("从已登记及当前范围内的仓库中选择")
        self.vault_list.setIcon(action_icon("search"))
        self.running_vault = QPushButton("当前运行")
        self.running_vault.setToolTip("识别当前正在 Obsidian 中运行的仓库")
        self.running_vault.setIcon(action_icon("refresh"))
        for button in (self.vault_list, self.running_vault, self.browse):
            button.setProperty("pathAction", True)
        self.actions_widget = QWidget()
        self.actions_widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        actions = QHBoxLayout(self.actions_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addSpacing(116)
        self.action_indent = actions.itemAt(0).spacerItem()
        actions.addWidget(self.vault_list)
        actions.addWidget(self.running_vault)
        actions.addWidget(self.browse)
        actions.addStretch()
        outer.addWidget(self.actions_widget)
        self._inline_actions = False
        self.selection = VaultSelection(self, self.set_value, self._browse, lambda: (self.value,))
        self.vault_list.clicked.connect(lambda: self.selection.start(False))
        self.running_vault.clicked.connect(lambda: self.selection.start(True))
        self.edit.textChanged.connect(self.changed)
        self.edit.textChanged.connect(self.edit.setToolTip)
        self.edit.setToolTip(value)

    @property
    def value(self) -> str:
        return self.edit.text().strip()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        inline = self.width() >= 700
        if inline != self._inline_actions:
            self._inline_actions = inline
            self.action_indent.changeSize(0 if inline else 116, 0)
            if inline:
                self.input_layout.insertWidget(2, self.actions_widget)
            else:
                self.outer_layout.addWidget(self.actions_widget)
            self.actions_widget.layout().invalidate()

    def set_value(self, value: str) -> None:
        self.edit.setText(value)
        self.edit.setToolTip(value)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.edit.setEnabled(enabled)
        self.browse.setEnabled(enabled)
        self.vault_list.setEnabled(enabled)
        self.running_vault.setEnabled(enabled)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, self.dialog_title, self.value)
        if chosen:
            self.set_value(os.path.normpath(chosen))


class FeaturePage(QWidget):
    """Shared page frame; domain services remain independent from Qt."""

    task_requested = Signal(object)
    cancel_requested = Signal()
    settings_changed = Signal()
    message_logged = Signal(str)

    def __init__(self, title: str, description: str) -> None:
        super().__init__()
        self._task_active = False
        self._global_busy = False
        self._external_recovery_pending = False
        self._task_kind = ""
        self._status_kind = ""
        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(22, 15, 22, 12)
        self.root_layout.setSpacing(10)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        heading.addWidget(title_label)
        subtitle = QLabel(description)
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(subtitle)
        header.addLayout(heading, 1)
        self.root_layout.addLayout(header)

        self.body = QVBoxLayout()
        self.body.setSpacing(9)
        self.root_layout.addLayout(self.body, 1)

        self.task_panel = QFrame()
        self.task_panel.setObjectName("Panel")
        task_layout = QVBoxLayout(self.task_panel)
        task_layout.setContentsMargins(14, 8, 14, 8)
        task_layout.setSpacing(5)
        row = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setFixedWidth(12)
        row.addWidget(self.status_dot)
        self.status_label = QLabel("就绪")
        self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)
        self.progress_detail = QLabel("")
        self.progress_detail.setObjectName("Muted")
        row.addWidget(self.progress_detail)
        task_layout.addLayout(row)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        task_layout.addWidget(self.progress_bar)
        self.root_layout.addWidget(self.task_panel)

    def settings_payload(self) -> dict[str, Any]:
        return {}

    def repository_paths(self) -> tuple[str, ...]:
        """Return configured repository/source roots used for state separation checks."""
        return ()

    def invalidate_confirmation(self) -> None:
        """Called when navigating away from a page with a destructive preview."""

    def set_global_busy(self, busy: bool) -> None:
        self._global_busy = busy
        self.refresh_actions()

    def set_external_recovery_pending(self, pending: bool) -> None:
        """Block new writes while another page owns an unfinished transaction."""
        self._external_recovery_pending = pending
        self.refresh_actions()

    def has_pending_recovery(self) -> bool:
        """Return whether this page owns unresolved or unreadable durable state."""
        return False

    def task_cancellable(self) -> bool:
        """Whether the active page operation actually observes its cancel event."""
        return self._task_active

    def start_task(self, kind: str, operation: Any) -> None:
        if self._global_busy:
            return
        self._task_kind = kind
        self._task_active = True
        self.progress_bar.setRange(0, 0)
        self.progress_detail.setText("")
        self.set_status("正在处理…", "busy")
        self.task_requested.emit(operation)
        self.refresh_actions()

    def task_progress(self, event: Progress) -> None:
        # Management engines share one lightweight progress protocol without
        # depending on Qt or on the mirror model's concrete dataclass.
        message = str(getattr(event, "message", "") or "")
        relative_path = str(getattr(event, "relative_path", "") or "")
        completed_bytes = int(getattr(event, "completed_bytes", 0) or 0)
        total_bytes = int(getattr(event, "total_bytes", 0) or 0)
        completed_files = int(getattr(event, "completed_files", 0) or 0)
        total_files = int(getattr(event, "total_files", 0) or 0)
        text = message or relative_path or "正在处理…"
        self.set_status(text, "busy")
        if total_bytes:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(completed_bytes * 1000 / total_bytes)))
            self.progress_detail.setText(
                f"{format_bytes(completed_bytes)} / {format_bytes(total_bytes)}"
            )
        elif total_files:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(completed_files * 1000 / total_files)))
            self.progress_detail.setText(f"{completed_files:,} / {total_files:,} 项")
        else:
            self.progress_bar.setRange(0, 0)

    def task_finished(self, status: str, payload: Any) -> None:
        self._task_active = False
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0 if status != "ok" else 1000)
        self.progress_detail.setText("")
        if status == "cancelled":
            self.set_status("操作已取消，执行前请重新分析。", "warning")
        elif status == "error":
            self.set_status(str(payload), "error")
        self.refresh_actions()

    def set_status(self, text: str, kind: str = "neutral") -> None:
        colors = {
            "neutral": "#8A9A9F",
            "busy": "#5E8AA2",
            "success": "#53916F",
            "warning": "#B38749",
            "error": "#B66B5D",
        }
        if self.status_label.text() == text and self._status_kind == kind:
            return
        self._status_kind = kind
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        self.status_dot.setStyleSheet(
            f"color: {colors.get(kind, colors['neutral'])}; font-size: 9px;"
        )

    def refresh_actions(self) -> None:
        pass


def panel(layout: QVBoxLayout) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Panel")
    inner = QVBoxLayout(frame)
    inner.setContentsMargins(14, 11, 14, 11)
    inner.setSpacing(8)
    layout.addWidget(frame)
    return frame, inner


def format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return "0 B"
