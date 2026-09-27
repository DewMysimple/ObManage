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

    def __init__(self, result: VaultCatalogResult, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择来源仓库")
        self.resize(780, 480)
        self.setMinimumSize(540, 340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        heading = QLabel("选择要复制 .obsidian 的来源仓库")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        hint = QLabel("包含 Obsidian 已登记的仓库（含已关闭）和当前集合内的仓库。")
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
        for vault in result.vaults:
            items = [QStandardItem(vault.name), QStandardItem(vault.path)]
            for item in items:
                item.setToolTip(vault.path)
                item.setEditable(False)
            self.model.appendRow(items)
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
        if result.issues:
            issues = QLabel(f"有 {len(result.issues)} 个读取问题：{result.issues[0].message}")
            issues.setWordWrap(True)
            issues.setObjectName("TargetEffect")
            issues.setToolTip("\n".join(f"{i.message} {i.path or ''}" for i in result.issues))
            layout.addWidget(issues)
        actions = QHBoxLayout()
        browse = QPushButton("浏览文件夹…")
        browse.setIcon(action_icon("folder"))
        browse.clicked.connect(self._browse)
        actions.addWidget(browse)
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
        self._filter("")

    def _filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text.strip())
        count = self.proxy.rowCount()
        self.summary.setText(
            f"{count} / {self.model.rowCount()} 个仓库 · 选中后点击「使用此仓库」"
            if count else "没有匹配的仓库；可更换搜索词，或直接浏览文件夹。"
        )
        self._update_choice()

    def _update_choice(self, *_args) -> None:
        self.choose_button.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def _choose(self, *_args) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            path = self.proxy.index(rows[0].row(), 1).data()
            self.source_chosen.emit(path)
            self.accept()

    def _browse(self) -> None:
        self.reject()
        self.browse_requested.emit()
