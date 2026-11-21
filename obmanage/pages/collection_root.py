from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from .icons import action_icon


class CollectionRootPicker(QWidget):
    """Configure a discovery directory independently of vault selection."""

    changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.input_layout = QHBoxLayout(self)
        self.input_layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("仓库集合根目录")
        label.setFixedWidth(106)
        self.input_layout.addWidget(label)
        self.edit = QLineEdit()
        self.edit.setReadOnly(True)
        label.setBuddy(self.edit)
        self.input_layout.addWidget(self.edit, 1)
        self.browse = QPushButton("更改目录")
        self.browse.setIcon(action_icon("folder"))
        self.browse.clicked.connect(self._browse)
        self.input_layout.addWidget(self.browse)
        self.edit.textChanged.connect(self.changed)
        self.edit.textChanged.connect(self.edit.setToolTip)

    @property
    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, value: str) -> None:
        self.edit.setText(value)
        self.edit.setCursorPosition(0)
        self.edit.setToolTip(value)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.browse.setEnabled(enabled)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "更改仓库集合根目录", self.value)
        if path:
            self.set_value(os.path.normpath(path))
