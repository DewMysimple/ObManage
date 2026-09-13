from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
)

from ..management import collect_vault_statistics, discover_vaults
from ..management.models import FileTypeStatistics, VaultStatistics, VaultStatisticsResult
from .common import FeaturePage, PathPicker, format_bytes, panel


class StatisticsTableModel(QAbstractTableModel):
    HEADERS = ("仓库", "完整位置", "文件", "文件夹", "总大小", "Markdown", "状态")

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
            f"{item.total_files:,}",
            f"{item.folders:,}",
            format_bytes(item.total_bytes),
            f"{item.markdown_files:,}",
            "完整" if item.complete else "部分统计",
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            type_details = "；".join(
                f"{entry.label} {entry.files:,} 个 / {format_bytes(entry.total_bytes)}"
                for entry in sorted(
                    item.file_types,
                    key=lambda entry: (-entry.total_bytes, entry.label.casefold(), entry.label),
                )
            )
            character_detail = (
                f"{item.utf8_characters:,}" if item.characters_counted else "未读取"
            )
            return (
                f"{item.vault_path}\n{values[6]}\n"
                f"文件 {item.total_files:,} · 文件夹 {item.folders:,} · "
                f"总大小 {format_bytes(item.total_bytes)}\n"
                f"Markdown {item.markdown_files:,} · 字符 {character_detail}"
                + (f"\n类型：{type_details}" if type_details else "")
            )
        if role == Qt.ItemDataRole.UserRole:
            if index.column() == 0:
                return (Path(item.vault_path).name.casefold(), item.vault_path.casefold())
            if index.column() == 1:
                return item.vault_path.casefold()
            if index.column() == 2:
                return item.total_files
            if index.column() == 3:
                return item.folders
            if index.column() == 4:
                return item.total_bytes
            if index.column() == 5:
                return item.markdown_files
            return 0 if item.complete else 1
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (2, 3, 4, 5):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 6 and not item.complete:
            from PySide6.QtGui import QColor
            return QColor("#AD5748")
        return None


class FileTypeTableModel(QAbstractTableModel):
    HEADERS = ("类别", "出现的扩展名", "文件", "大小", "占总大小")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[FileTypeStatistics] = []
        self.total_bytes = 0

    def set_rows(self, rows: list[FileTypeStatistics], total_bytes: int) -> None:
        self.beginResetModel()
        self.rows = rows
        self.total_bytes = total_bytes
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
        extensions = "、".join(item.extensions)
        share = item.total_bytes / self.total_bytes if self.total_bytes else 0.0
        values = (
            item.label,
            extensions,
            f"{item.files:,}",
            format_bytes(item.total_bytes),
            f"{share:.1%}",
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"{item.label}\n扩展名：{extensions}\n"
                f"文件 {item.files:,} · 大小 {format_bytes(item.total_bytes)} · "
                f"占当前范围 {share:.1%}"
            )
        if role == Qt.ItemDataRole.UserRole:
            if index.column() == 0:
                return item.label.casefold()
            if index.column() == 1:
                return extensions.casefold()
            if index.column() == 2:
                return item.files
            if index.column() == 3:
                return item.total_bytes
            return share
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (2, 3, 4):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None


class StatisticsPage(FeaturePage):
    def __init__(self, settings: dict[str, Any], default_root: str) -> None:
        super().__init__("仓库统计", "查看仓库容量、文件类型、Markdown 笔记与 UTF-8 字符；全程只读。")
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
        self.count_characters.setToolTip("关闭后仍统计全部文件与容量，但不打开 Markdown 内容，速度更快。")
        options.addWidget(self.count_characters)
        self.include_trash = QCheckBox("包含根级 .trash 内容")
        self.include_trash.setChecked(bool(settings.get("include_trash", False)))
        options.addWidget(self.include_trash)
        options.addStretch()
        config.addLayout(options)

        summary_frame, summary = panel(self.body)
        summary_frame.setObjectName("Panel")
        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(20)
        summary_grid.setVerticalSpacing(6)
        self.summary_values: dict[str, QLabel] = {}
        for index, (key, title) in enumerate((
            ("vaults", "仓库"), ("files", "全部文件"), ("size", "内容总大小"),
            ("notes", "Markdown"), ("characters", "字符"), ("issues", "警告"),
        )):
            block = QHBoxLayout()
            caption = QLabel(title)
            caption.setObjectName("Muted")
            value = QLabel("—")
            value.setObjectName("StatValue")
            block.addWidget(caption)
            block.addWidget(value)
            summary_grid.addLayout(block, index // 3, index % 3)
            summary_grid.setColumnStretch(index % 3, 1)
            self.summary_values[key] = value
        summary.addLayout(summary_grid)

        table_frame, table_layout = panel(self.body)
        table_frame.setSizePolicy(table_frame.sizePolicy().horizontalPolicy(),
                                  table_frame.sizePolicy().verticalPolicy())
        header = QHBoxLayout()
        title = QLabel("逐仓库统计")
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
        self.table.sortByColumn(4, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header_view = self.table.horizontalHeader()
        header_view.setStretchLastSection(False)
        header_view.setSectionResizeMode(0, header_view.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, header_view.ResizeMode.Stretch)
        for column in (2, 3, 4, 5, 6):
            header_view.setSectionResizeMode(column, header_view.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(190)
        table_layout.addWidget(self.table, 1)
        self.issues_label = QLabel("")
        self.issues_label.setObjectName("Muted")
        self.issues_label.setWordWrap(True)
        self.issues_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        table_layout.addWidget(self.issues_label)
        type_frame, type_layout = panel(self.body)
        type_header = QHBoxLayout()
        type_title = QLabel("文件类型构成")
        type_title.setObjectName("SectionTitle")
        type_header.addWidget(type_title)
        self.type_scope_label = QLabel("全部仓库")
        self.type_scope_label.setObjectName("Muted")
        type_header.addWidget(self.type_scope_label)
        type_header.addStretch()
        self.show_all_types_button = QPushButton("显示全部仓库")
        self.show_all_types_button.setObjectName("TextButton")
        type_header.addWidget(self.show_all_types_button)
        type_layout.addLayout(type_header)
        self.type_table = QTableView()
        self.type_table.setObjectName("statistics_type_table")
        self.type_model = FileTypeTableModel(self)
        self.type_proxy = QSortFilterProxyModel(self)
        self.type_proxy.setSourceModel(self.type_model)
        self.type_proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.type_table.setModel(self.type_proxy)
        self.type_table.setSortingEnabled(True)
        self.type_table.sortByColumn(3, Qt.SortOrder.DescendingOrder)
        self.type_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.type_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.type_table.setAlternatingRowColors(True)
        self.type_table.verticalHeader().hide()
        type_header_view = self.type_table.horizontalHeader()
        type_header_view.setStretchLastSection(False)
        type_header_view.setSectionResizeMode(0, type_header_view.ResizeMode.ResizeToContents)
        type_header_view.setSectionResizeMode(1, type_header_view.ResizeMode.Stretch)
        for column in (2, 3, 4):
            type_header_view.setSectionResizeMode(column, type_header_view.ResizeMode.ResizeToContents)
        self.type_table.setMinimumHeight(210)
        type_layout.addWidget(self.type_table, 1)
        type_hint = QLabel("按扩展名归类；所有普通文件都会计入，无法识别的类型归入“其他文件”。")
        type_hint.setObjectName("Muted")
        type_hint.setWordWrap(True)
        type_layout.addWidget(type_hint)
        self.body.setStretch(2, 1)
        self.body.setStretch(3, 1)

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
        self.table.selectionModel().selectionChanged.connect(self._vault_selection_changed)
        self.show_all_types_button.clicked.connect(self.table.clearSelection)
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
            key=lambda item: (-item.total_bytes, item.vault_path.casefold(), item.vault_path),
        )
        self.model.set_rows(rows)
        vaults = len(rows)
        files = sum(item.total_files for item in rows)
        total_bytes = sum(item.total_bytes for item in rows)
        notes = sum(item.markdown_files for item in rows)
        characters = sum(item.utf8_characters for item in rows)
        self.summary_values["vaults"].setText(f"{vaults:,}")
        self.summary_values["files"].setText(f"{files:,}")
        self.summary_values["size"].setText(format_bytes(total_bytes))
        self.summary_values["notes"].setText(f"{notes:,}")
        self.summary_values["characters"].setText(
            f"{characters:,}" if self.count_characters.isChecked() else "未读取"
        )
        self.summary_values["issues"].setText(f"{len(result.issues):,}")
        self.table.clearSelection()
        self._refresh_type_rows()
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
            self.set_status(
                f"统计完成：{vaults} 个仓库，{files:,} 个文件，"
                f"内容总大小 {format_bytes(total_bytes)}。",
                "success",
            )
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
        self.show_all_types_button.setEnabled(available and bool(self.table.selectionModel().selectedRows()))

    @Slot()
    def _vault_selection_changed(self, *_: Any) -> None:
        self._refresh_type_rows()
        self.refresh_actions()

    def _selected_vaults(self) -> list[VaultStatistics]:
        rows: list[VaultStatistics] = []
        for index in self.table.selectionModel().selectedRows():
            source = self.proxy.mapToSource(index)
            if 0 <= source.row() < len(self.model.rows):
                rows.append(self.model.rows[source.row()])
        return rows

    def _refresh_type_rows(self) -> None:
        selected = self._selected_vaults()
        if selected:
            rows = list(VaultStatisticsResult(statistics=tuple(selected)).file_types)
            total_bytes = sum(item.total_bytes for item in selected)
            if len(selected) == 1:
                name = Path(selected[0].vault_path).name or selected[0].vault_path
                display_name = self.type_scope_label.fontMetrics().elidedText(
                    name, Qt.TextElideMode.ElideMiddle, 200
                )
                self.type_scope_label.setText(f"当前仓库：{display_name}")
                self.type_scope_label.setToolTip(selected[0].vault_path)
            else:
                self.type_scope_label.setText(f"已选 {len(selected):,} 个仓库")
                self.type_scope_label.setToolTip("\n".join(item.vault_path for item in selected))
        elif self._result is not None:
            rows = list(self._result.file_types)
            total_bytes = sum(item.total_bytes for item in self._result.statistics)
            self.type_scope_label.setText("全部仓库")
            self.type_scope_label.setToolTip("")
        else:
            rows = []
            total_bytes = 0
            self.type_scope_label.setText("全部仓库")
            self.type_scope_label.setToolTip("")
        rows.sort(key=lambda item: (-item.total_bytes, -item.files, item.label.casefold()))
        self.type_model.set_rows(rows, total_bytes)

    def copy_selected_paths(self) -> None:
        paths = []
        for index in self.table.selectionModel().selectedRows():
            source = self.proxy.mapToSource(index)
            paths.append(self.model.rows[source.row()].vault_path)
        if paths:
            QApplication.clipboard().setText("\n".join(paths))
