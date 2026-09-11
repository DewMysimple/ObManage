from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
)

from ..management import collect_vault_statistics, discover_vaults
from ..management.models import VaultStatistics, VaultStatisticsResult
from .common import FeaturePage, PathPicker, format_bytes, panel


class StatisticsTableModel(QAbstractTableModel):
    HEADERS = ("仓库", "完整位置", "Markdown", "字符", "Markdown 大小", "状态")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[VaultStatistics] = []

    def set_rows(self, rows: list[VaultStatistics]) -> None:
        self.beginResetModel()
        self.rows = rows
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
        values = (
            Path(item.vault_path).name or item.vault_path,
            item.vault_path,
            f"{item.markdown_files:,}",
            f"{item.utf8_characters:,}" if item.characters_counted else "未读取",
            format_bytes(item.markdown_bytes),
            "完整" if item.complete else "部分统计",
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{item.vault_path}\n{values[5]}"
        if role == Qt.ItemDataRole.UserRole:
            if index.column() == 0:
                return (Path(item.vault_path).name.casefold(), item.vault_path.casefold())
            if index.column() == 1:
                return item.vault_path.casefold()
            if index.column() == 2:
                return item.markdown_files
            if index.column() == 3:
                return item.utf8_characters if item.characters_counted else -1
            if index.column() == 4:
                return item.markdown_bytes
            return 0 if item.complete else 1
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (2, 3, 4):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 5 and not item.complete:
            from PySide6.QtGui import QColor
            return QColor("#AD5748")
        return None


class StatisticsPage(FeaturePage):
    def __init__(self, settings: dict[str, Any], default_root: str) -> None:
        super().__init__("仓库统计", "一次扫描统计仓库、Markdown 笔记与 UTF-8 字符；全程只读。")
        self._result: VaultStatisticsResult | None = None

        _, config = panel(self.body)
        self.root_picker = PathPicker(
            "仓库集合根目录",
            str(settings.get("root") or default_root),
            dialog_title="选择要统计的仓库集合根目录",
        )
        self.root_picker.edit.setObjectName("statistics_root")
        config.addWidget(self.root_picker)
        options = QHBoxLayout()
        options.addSpacing(116)
        self.count_characters = QCheckBox("读取内容并统计 UTF-8 字符")
        self.count_characters.setChecked(bool(settings.get("count_characters", True)))
        self.count_characters.setToolTip("关闭后只统计文件数量与字节，不打开 Markdown 内容，速度更快。")
        options.addWidget(self.count_characters)
        self.include_trash = QCheckBox("包含根级 .trash 中的 Markdown")
        self.include_trash.setChecked(bool(settings.get("include_trash", False)))
        options.addWidget(self.include_trash)
        options.addStretch()
        config.addLayout(options)

        summary_frame, summary = panel(self.body)
        summary_frame.setObjectName("Panel")
        summary_row = QHBoxLayout()
        self.summary_values: dict[str, QLabel] = {}
        for key, title in (("vaults", "仓库"), ("notes", "Markdown"),
                           ("characters", "字符"), ("issues", "警告")):
            block = QHBoxLayout()
            caption = QLabel(title)
            caption.setObjectName("Muted")
            value = QLabel("—")
            value.setObjectName("StatValue")
            block.addWidget(caption)
            block.addWidget(value)
            summary_row.addLayout(block)
            summary_row.addStretch()
            self.summary_values[key] = value
        summary.addLayout(summary_row)

        table_frame, table_layout = panel(self.body)
        table_frame.setSizePolicy(table_frame.sizePolicy().horizontalPolicy(),
                                  table_frame.sizePolicy().verticalPolicy())
        header = QHBoxLayout()
        title = QLabel("统计结果")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch()
        self.copy_button = QPushButton("复制所选路径")
        self.copy_button.setObjectName("TextButton")
        header.addWidget(self.copy_button)
        table_layout.addLayout(header)
        self.table = QTableView()
        self.table.setObjectName("statistics_table")
        self.model = StatisticsTableModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header_view = self.table.horizontalHeader()
        header_view.setStretchLastSection(False)
        header_view.setSectionResizeMode(0, header_view.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, header_view.ResizeMode.Stretch)
        for column in (2, 3, 4, 5):
            header_view.setSectionResizeMode(column, header_view.ResizeMode.ResizeToContents)
        table_layout.addWidget(self.table, 1)
        self.issues_label = QLabel("")
        self.issues_label.setObjectName("Muted")
        self.issues_label.setWordWrap(True)
        self.issues_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        table_layout.addWidget(self.issues_label)
        self.body.setStretch(self.body.count() - 1, 1)

        actions = QHBoxLayout()
        self.scope_hint = QLabel("默认排除 .obsidian、根级 .trash 与嵌套子仓库，避免重复统计。")
        self.scope_hint.setObjectName("Muted")
        actions.addWidget(self.scope_hint, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Cancel")
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        self.scan_button = QPushButton("开始统计")
        self.scan_button.setObjectName("Primary")
        actions.addWidget(self.scan_button)
        self.root_layout.addLayout(actions)

        self.root_picker.changed.connect(self._inputs_changed)
        self.count_characters.toggled.connect(self._inputs_changed)
        self.include_trash.toggled.connect(self._inputs_changed)
        self.scan_button.clicked.connect(self._start_scan)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.copy_button.clicked.connect(self.copy_selected_paths)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self.refresh_actions())
        self.refresh_actions()

    def settings_payload(self) -> dict[str, Any]:
        return {
            "root": self.root_picker.value,
            "count_characters": self.count_characters.isChecked(),
            "include_trash": self.include_trash.isChecked(),
        }

    def repository_paths(self) -> tuple[str, ...]:
        return (self.root_picker.value,) if self.root_picker.value else ()

    @Slot()
    def _inputs_changed(self, *_: Any) -> None:
        self.settings_changed.emit()
        if not self._task_active:
            self.set_status("统计范围已改变，点击「开始统计」刷新结果。")

    def _start_scan(self) -> None:
        root = self.root_picker.value
        count_characters = self.count_characters.isChecked()
        include_trash = self.include_trash.isChecked()

        def operation(cancel, progress):
            catalog = discover_vaults(root, cancel=cancel, progress=progress)
            return collect_vault_statistics(
                catalog,
                count_characters=count_characters,
                include_trash=include_trash,
                cancel=cancel,
                progress=progress,
            )

        self.start_task("statistics", operation)

    def task_finished(self, status: str, payload: Any) -> None:
        super().task_finished(status, payload)
        if status != "ok":
            return
        result: VaultStatisticsResult = payload
        self._result = result
        rows = sorted(
            result.statistics,
            key=lambda item: (-item.markdown_files, item.vault_path.casefold(), item.vault_path),
        )
        self.model.set_rows(rows)
        vaults = len(rows)
        notes = sum(item.markdown_files for item in rows)
        characters = sum(item.utf8_characters for item in rows)
        self.summary_values["vaults"].setText(f"{vaults:,}")
        self.summary_values["notes"].setText(f"{notes:,}")
        self.summary_values["characters"].setText(
            f"{characters:,}" if self.count_characters.isChecked() else "未读取"
        )
        self.summary_values["issues"].setText(f"{len(result.issues):,}")
        if result.issues:
            first = result.issues[0]
            self.issues_label.setText(f"首个问题：{first.message}")
            self.issues_label.setToolTip("\n".join(
                f"{issue.message}{' · ' + issue.path if issue.path else ''}"
                for issue in result.issues
            ))
        else:
            self.issues_label.setText("")
            self.issues_label.setToolTip("")
        if not rows:
            detail = result.issues[0].message if result.issues else "没有找到包含 .obsidian 的仓库。"
            self.set_status(detail, "warning")
        elif result.issues:
            self.set_status(
                f"已统计 {vaults} 个仓库；有 {len(result.issues)} 项无法完整读取，结果已明确标记。",
                "warning",
            )
        else:
            self.set_status(f"统计完成：{vaults} 个仓库，{notes:,} 篇 Markdown。", "success")
        self.message_logged.emit(self.status_label.text())
        self.refresh_actions()

    def refresh_actions(self) -> None:
        available = not self._global_busy and not self._task_active
        self.root_picker.set_controls_enabled(available)
        self.count_characters.setEnabled(available)
        self.include_trash.setEnabled(available)
        self.scan_button.setEnabled(available and bool(self.root_picker.value))
        self.cancel_button.setVisible(self._task_active)
        self.cancel_button.setEnabled(self._task_active)
        self.copy_button.setEnabled(available and bool(self.table.selectionModel().selectedRows()))

    def copy_selected_paths(self) -> None:
        paths = []
        for index in self.table.selectionModel().selectedRows():
            source = self.proxy.mapToSource(index)
            paths.append(self.model.rows[source.row()].vault_path)
        if paths:
            QApplication.clipboard().setText("\n".join(paths))
