from __future__ import annotations

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableView, QVBoxLayout,
)

from ..management.models import VaultCatalogResult
from .icons import action_icon


class VaultSourceDialog(QDialog):
    """Searchable, explicit choice; opening a list never selects a source."""

    source_chosen = Signal(str)
    browse_requested = Signal()
    first_painted = Signal()

    def __init__(self, result: VaultCatalogResult, parent=None, *, running=False) -> None:
        super().__init__(parent)
        self.setWindowTitle("当前运行的仓库" if running else "从仓库列表选择")
        self.resize(780, 480)
        self.setMinimumSize(540, 340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        heading = QLabel("选择当前运行的仓库" if running else "选择仓库")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        hint = QLabel("已核验 Obsidian 进程和当前仓库窗口；请核对完整位置。" if running else
                      "包含 Obsidian 已登记的仓库（含已关闭）和当前选择范围内的仓库。")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索仓库名称或完整路径…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索来源仓库")
        self.search.addAction(action_icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        layout.addWidget(self.search)
        self.model = QStandardItemModel(0, 2, self)
        self.model.setHorizontalHeaderLabels(("仓库", "完整位置"))
        self._selectable = True
        self._deferred = False
        self._painted = False
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 180)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setObjectName("Muted")
        layout.addWidget(self.summary)
        self.freshness = QLabel()
        self.freshness.setWordWrap(True)
        self.freshness.setObjectName("TargetEffect")
        layout.addWidget(self.freshness)
        self.freshness.hide()
        self.issues = QLabel()
        self.issues.setWordWrap(True)
        self.issues.setObjectName("TargetEffect")
        layout.addWidget(self.issues)
        actions = QHBoxLayout()
        self.browse_button = QPushButton("浏览文件夹…")
        self.browse_button.setIcon(action_icon("folder"))
        self.browse_button.clicked.connect(self._browse)
        actions.addWidget(self.browse_button)
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.choose_button = QPushButton("使用此仓库")
        self.choose_button.setObjectName("Primary")
        self.choose_button.setDefault(True)
        self.choose_button.clicked.connect(self._choose)
        actions.addWidget(self.choose_button)
        layout.addLayout(actions)
        self.search.textChanged.connect(self._filter)
        self.table.selectionModel().selectionChanged.connect(self._update_choice)
        self.table.doubleClicked.connect(self._choose)
        self.set_result(result)

    def set_result(self, result, selected=None):
        self.model.removeRows(0, self.model.rowCount())
        for vault in result.vaults:
            items = [QStandardItem(vault.name), QStandardItem(vault.path)]
            for item in items:
                item.setToolTip(vault.path)
                item.setEditable(False)
            self.model.appendRow(items)
        self.issues.setVisible(bool(result.issues))
        if result.issues:
            self.issues.setText(f"有 {len(result.issues)} 个读取问题：{result.issues[0].message}")
            self.issues.setToolTip("\n".join(f"{i.message} {i.path or ''}" for i in result.issues))
        self._filter(self.search.text())
        if selected is not None:
            for row in range(self.proxy.rowCount()):
                if self.proxy.index(row, 1).data() == selected:
                    self.table.selectRow(row)
                    break

    def set_freshness(self, text, *, selectable=True, deferred=False):
        self.freshness.setText(text)
        self.freshness.setVisible(bool(text))
        self._selectable = selectable
        self._deferred = deferred
        self.browse_button.setEnabled(not deferred)
        self._update_choice()

    def selected_path(self):
        rows = self.table.selectionModel().selectedRows()
        return self.proxy.index(rows[0].row(), 1).data() if rows else None

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._painted and self.model.rowCount():
            self._painted = True
            self.first_painted.emit()

    def _filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text.strip())
        count = self.proxy.rowCount()
        self.summary.setText(
            f"{count} / {self.model.rowCount()} 个仓库 · 选中后点击「使用此仓库」"
            if count else "没有匹配的仓库；可更换搜索词，或直接浏览文件夹。"
        )
        self._update_choice()

    def _update_choice(self, *_args) -> None:
        self.choose_button.setEnabled(self._selectable and self.selected_path() is not None)

    def _choose(self, *_args) -> None:
        path = self.selected_path()
        if self._selectable and path:
            deferred = self._deferred
            self.source_chosen.emit(path)
            if not deferred:
                self.accept()

    def _browse(self) -> None:
        self.reject()
        self.browse_requested.emit()
