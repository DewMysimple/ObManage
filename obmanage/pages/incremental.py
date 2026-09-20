from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHeaderView, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTabBar,
)

from ..management.incremental import (
    IncrementalAnalysis, IncrementalEngine, IncrementalResult, selected_plans,
)
from ..models import SyncError
from .backup import BackupPlanTableModel, BackupPlanTableView
from .common import FeaturePage, PathPicker, format_bytes, panel


class IncrementalPage(FeaturePage):
    """Choose an authoritative endpoint independently for each differing vault."""

    def __init__(self, state_dir: Path, settings: dict[str, Any],
                 default_local: str, default_portable: str) -> None:
        super().__init__(
            "仓库增量处理",
            "按相对路径配对，只列有差异的仓库；逐个指定来源，可在同一批次选择不同方向。",
        )
        self.state_dir = Path(state_dir)
        self.analysis: IncrementalAnalysis | None = None
        self.last_result: IncrementalResult | None = None
        self.choices: dict[str, str] = {}
        self.source_selectors: dict[str, QComboBox] = {}
        _, inputs = panel(self.body)
        local = settings.get("local_path", default_local)
        portable = settings.get("portable_path", default_portable)
        self.local_picker = PathPicker("电脑仓库集合", local if isinstance(local, str) else default_local)
        self.portable_picker = PathPicker("移动硬盘集合", portable if isinstance(portable, str) else default_portable)
        inputs.addWidget(self.local_picker)
        inputs.addWidget(self.portable_picker)
        actions = QHBoxLayout()
        self.analyze_button = QPushButton("扫描对应仓库差异")
        self.analyze_button.setObjectName("Primary")
        actions.addWidget(self.analyze_button)
        hint = QLabel("包含视频、配置和 .trash；相同仓库自动隐藏。")
        hint.setWordWrap(True)
        actions.addWidget(hint, 1)
        inputs.addLayout(actions)

        _, vaults = panel(self.body)
        self.summary_label = QLabel("选择两端集合目录后扫描；程序不判断哪一端更新。")
        self.summary_label.setWordWrap(True)
        vaults.addWidget(self.summary_label)
        self.vault_table = QTableWidget(0, 4)
        self.vault_table.setHorizontalHeaderLabels(("对应仓库", "差异（文件与目录）", "本轮来源", "状态"))
        self.vault_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.vault_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.vault_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.vault_table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.vault_table.verticalHeader().hide()
        self.vault_table.verticalHeader().setDefaultSectionSize(36)
        self.vault_table.setMinimumHeight(160)
        self.vault_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.vault_table.horizontalHeader()
        header.setMinimumSectionSize(40)
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(2, 158)
        vaults.addWidget(self.vault_table, 1)
        self.issues_label = QLabel()
        self.issues_label.setWordWrap(True)
        self.issues_label.setMinimumWidth(0)
        self.issues_label.hide()
        vaults.addWidget(self.issues_label)

        _, details = panel(self.body)
        self.preview_title = QLabel("选中仓库，可分别查看两种来源方案的影响")
        self.preview_title.setWordWrap(True)
        details.addWidget(self.preview_title)
        self.preview_tabs = QTabBar()
        self.preview_tabs.addTab("方案：电脑为源")
        self.preview_tabs.addTab("方案：移动硬盘为源")
        details.addWidget(self.preview_tabs)
        self.model = BackupPlanTableModel(self)
        self.table = BackupPlanTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(190)
        details.addWidget(self.table, 1)

        _, footer = panel(self.body)
        self.selection_label = QLabel("请选择来源；所选仓库范围包含其嵌套子仓库。")
        self.selection_label.setWordWrap(True)
        footer.addWidget(self.selection_label)
        self.confirm_checkbox = QCheckBox("我已核对各仓库来源及删除明细，确认执行所选方向")
        footer.addWidget(self.confirm_checkbox)
        buttons = QHBoxLayout()
        self.execute_button = QPushButton("执行所选仓库更新")
        self.execute_button.setObjectName("Primary")
        self.cancel_button = QPushButton("取消")
        buttons.addWidget(self.execute_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        footer.addLayout(buttons)

        self.local_picker.changed.connect(self._inputs_changed)
        self.portable_picker.changed.connect(self._inputs_changed)
        self.analyze_button.clicked.connect(self._start_scan)
        self.execute_button.clicked.connect(self._start_execute)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.confirm_checkbox.toggled.connect(self.refresh_actions)
        self.vault_table.currentCellChanged.connect(self._current_pair_changed)
        self.preview_tabs.currentChanged.connect(self._refresh_preview)
        self.refresh_actions()

    def settings_payload(self) -> dict[str, Any]:
        return {"local_path": self.local_picker.value, "portable_path": self.portable_picker.value}

    def repository_paths(self) -> tuple[str, ...]:
        return tuple(value for value in (self.local_picker.value, self.portable_picker.value) if value)

    def _paths_match(self) -> bool:
        def normalized(path):
            return os.path.normcase(os.path.realpath(os.path.expanduser(path)))
        return self.analysis is not None and (
            normalized(self.local_picker.value), normalized(self.portable_picker.value)
        ) == (normalized(self.analysis.local_root), normalized(self.analysis.portable_root))

    def invalidate_confirmation(self) -> None:
        self.confirm_checkbox.setChecked(False)

    def _clear_analysis(self) -> None:
        self.analysis = None
        self.last_result = None
        self.choices.clear()
        self.source_selectors.clear()
        self.confirm_checkbox.setChecked(False)
        self.vault_table.setRowCount(0)
        self.model.set_rows([])
        self.preview_title.setText("选中仓库，可分别查看两种来源方案的影响")
        self.issues_label.hide()
        self.summary_label.setText("尚未扫描；所有仓库来源需逐个选择。")
        self.refresh_actions()

    def _inputs_changed(self, *_: Any) -> None:
        self._clear_analysis()
        self.settings_changed.emit()
        self.set_status("路径已改变，请重新扫描对应仓库。")

    def _start_scan(self) -> None:
        if self._task_active or self._global_busy:
            return
        local, portable = self.local_picker.value, self.portable_picker.value
        if not local or not portable:
            self.set_status("请先选择电脑和移动硬盘的仓库集合目录。", "warning")
            return
        self._clear_analysis()
        state = self.state_dir
        self.start_task("scan", lambda cancel, progress: IncrementalEngine(state).analyze(
            local, portable, cancel=cancel, progress=progress))
        if self._task_active:
            self.message_logged.emit("开始扫描对应仓库差异；两端仓库只读取。")

    def _accept_analysis(self, analysis: IncrementalAnalysis) -> None:
        self._clear_analysis()
        self.analysis = analysis
        if not self._paths_match():
            self._clear_analysis()
            self.set_status("扫描期间路径已改变，请重新扫描。", "warning")
            return
        self.vault_table.setRowCount(len(analysis.pairs))
        for row, pair in enumerate(analysis.pairs):
            plan = pair.local_plan or pair.portable_plan
            counts = plan.counts if plan else {}
            local_only = counts.get("add", 0) + counts.get("mkdir", 0)
            portable_only = counts.get("delete", 0) + counts.get("rmdir", 0)
            if pair.local_plan is None:
                local_only, portable_only = portable_only, local_only
            differences = counts.get("update", 0) + counts.get("rename", 0)
            summary = f"电脑独有 {local_only} / 硬盘独有 {portable_only} / 不同 {differences}"
            tooltip = (f"对应路径：{pair.relative_path}\n电脑：{pair.local_path}\n"
                       f"移动硬盘：{pair.portable_path}\n{summary}\n"
                       "按内容与目录结构比较，不以数量或时间判定新旧。")
            for column, value in ((0, pair.relative_path), (1, summary),
                                  (3, "无法处理" if pair.errors else "待选择")):
                item = QTableWidgetItem(value)
                item.setToolTip("\n".join(pair.errors) if pair.errors and column == 3 else tooltip)
                self.vault_table.setItem(row, column, item)
            selector = QComboBox()
            selector.addItem("暂不处理", "")
            if not pair.errors:
                for side, label in (("local", "电脑为源"), ("portable", "移动硬盘为源")):
                    selected = pair.plan_for(side)
                    if selected and selected.can_execute and selected.has_changes:
                        selector.addItem(label, side)
            selector.setToolTip("由你指定此仓库的来源；目标将按来源新增、覆盖和删除，含其中的嵌套子仓库。")
            self.source_selectors[pair.key] = selector
            self.vault_table.setCellWidget(row, 2, selector)
            selector.currentIndexChanged.connect(lambda _index, key=pair.key: self._choice_changed(key))
        self.summary_label.setText(
            f"待查看 {len(analysis.pairs)} 个仓库；已隐藏 {analysis.identical_count} 个完全一致的仓库。"
        )
        issues = [*analysis.issues, *(error for pair in analysis.pairs for error in pair.errors)]
        self.issues_label.setText("\n".join(issues))
        self.issues_label.setVisible(bool(issues))
        if analysis.pairs:
            self.vault_table.selectRow(0)
            self._current_pair_changed()
        self.set_status("扫描完成，请逐个选择来源。" if analysis.pairs else "两端对应仓库一致，无需更新。",
                        "success")
        if issues:
            self.set_status("扫描存在问题；异常仓库不能执行，请查看明细。", "warning")
        self.message_logged.emit(self.summary_label.text())
        self.refresh_actions()

    def _choice_changed(self, key: str) -> None:
        side = self.source_selectors[key].currentData()
        if side:
            self.choices[key] = side
        else:
            self.choices.pop(key, None)
        self.confirm_checkbox.setChecked(False)
        if self.analysis:
            row = next(index for index, pair in enumerate(self.analysis.pairs) if pair.key == key)
            self.vault_table.item(row, 3).setText("待执行" if side else "待选择")
            self.vault_table.selectRow(row)
            self._current_pair_changed()
        self.refresh_actions()

    def _current_pair_changed(self, *_: Any) -> None:
        row = self.vault_table.currentRow()
        if self.analysis and 0 <= row < len(self.analysis.pairs):
            pair = self.analysis.pairs[row]
            side = self.choices.get(pair.key, "local" if pair.local_plan else "portable")
            self.preview_tabs.setCurrentIndex(0 if side == "local" else 1)
        self._refresh_preview()

    def _refresh_preview(self, *_: Any) -> None:
        row = self.vault_table.currentRow()
        if self.analysis is None or not 0 <= row < len(self.analysis.pairs):
            self.model.set_rows([])
            return
        pair = self.analysis.pairs[row]
        side = "local" if self.preview_tabs.currentIndex() == 0 else "portable"
        plan = pair.plan_for(side)
        source, target = ("电脑", "移动硬盘") if side == "local" else ("移动硬盘", "电脑")
        chosen = self.choices.get(pair.key)
        selection = "本轮已选" if chosen == side else "仅供查看，未选择此方案"
        self.preview_title.setText(f"{pair.relative_path} · {source} → {target} · {selection}")
        self.preview_title.setToolTip(f"电脑：{pair.local_path}\n移动硬盘：{pair.portable_path}")
        if plan:
            self.model.set_context(plan.source, plan.target, source, target)
            self.model.set_rows([item for item in plan.items if item.action != "skip"])
        else:
            self.model.set_rows([])
            self.preview_title.setText(f"{pair.relative_path} · 此来源不可用（仓库缺失或存在扫描问题）")
        self.table.fit_columns()

    def _selected(self):
        if not self._paths_match() or self.analysis is None:
            raise SyncError("请先扫描当前路径。")
        return selected_plans(self.analysis, self.choices)

    def _start_execute(self) -> None:
        if self._task_active or self._global_busy or self._external_recovery_pending:
            return
        try:
            selected = self._selected()
        except SyncError as exc:
            self.set_status(str(exc), "warning")
            return
        if not self.confirm_checkbox.isChecked():
            return
        empty = [pair.relative_path for pair, plan in selected if plan.source_empty
                 and any(item.action in {"delete", "rmdir"} for item in plan.items)]
        if empty and QMessageBox.warning(
            self, "空来源将清空对应目标", "以下仓库的来源为空：\n" + "\n".join(empty)
            + "\n继续将清除预览中的目标内容，确认继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        analysis, choices, state = self.analysis, dict(self.choices), self.state_dir
        self.confirm_checkbox.setChecked(False)
        self.start_task("execute", lambda cancel, progress: IncrementalEngine(state).execute(
            analysis, choices, allow_empty=bool(empty), cancel=cancel, progress=progress))
        if self._task_active:
            for pair, plan in selected:
                self.message_logged.emit(f"仓库增量更新 {pair.relative_path}：{plan.source} → {plan.target}；"
                                         f"复制 {format_bytes(plan.bytes_to_copy)}。")

    def task_finished(self, status: str, payload: Any) -> None:
        kind = self._task_kind
        super().task_finished(status, payload)
        if kind == "scan" and status == "ok":
            self._accept_analysis(payload)
        elif kind == "execute":
            if status == "ok":
                self.last_result = payload
                outcomes = {outcome.key: outcome.result for outcome in payload.outcomes}
                if self.analysis:
                    for row, pair in enumerate(self.analysis.pairs):
                        result = outcomes.get(pair.key)
                        if result:
                            text = (f"{'完成' if result.status == 'success' else '未完成'} · "
                                    f"复制 {result.copied_files} / 删 {result.deleted_files + result.deleted_dirs}")
                            self.vault_table.item(row, 3).setText(text)
                            self.vault_table.item(row, 3).setToolTip(text + "\n" + "\n".join(result.errors))
                        elif pair.key in self.choices:
                            self.vault_table.item(row, 3).setText("未执行")
                completed = sum(item.result.status == "success" for item in payload.outcomes)
                summary = (f"完成 {completed}/{len(self.choices)} 个仓库；"
                           + ("请重新扫描核对。" if payload.status == "success"
                              else "任务已停止，已完成部分保留；请重新扫描。"))
                if payload.errors:
                    summary += " " + payload.errors[0]
                self.set_status(summary, "success" if payload.status == "success" else "warning")
            else:
                self.set_status("任务已停止，可能已有部分修改；请重新扫描。 " + str(payload or ""), "error")
            # Keep the preview as an execution record, but revoke all write authority.
            self.choices.clear()
            self.confirm_checkbox.setChecked(False)
            for selector in self.source_selectors.values():
                selector.setEnabled(False)
            self.analysis = None
            self.message_logged.emit(self.status_label.text())
        self._task_kind = ""
        self.refresh_actions()

    def refresh_actions(self, *_: Any) -> None:
        if not hasattr(self, "execute_button"):
            return
        available = not self._task_active and not self._global_busy
        self.local_picker.set_controls_enabled(available)
        self.portable_picker.set_controls_enabled(available)
        self.analyze_button.setEnabled(available and bool(self.local_picker.value and self.portable_picker.value))
        for selector in self.source_selectors.values():
            selector.setEnabled(available and self.analysis is not None and selector.count() > 1)
        executable = False
        try:
            selected = self._selected()
            counts = [plan.counts for _, plan in selected]
            deleted = sum(count["delete"] + count["rmdir"] for count in counts)
            size = sum(plan.bytes_to_copy for _, plan in selected)
            self.selection_label.setText(f"已选 {len(selected)} 个仓库 · 复制 {format_bytes(size)} · "
                                         f"从各自目标删除 {deleted} 项；范围含嵌套子仓库。")
            self.selection_label.setToolTip("\n".join(f"{pair.relative_path}：{plan.source} → {plan.target}"
                                                       for pair, plan in selected))
            executable = available and not self._external_recovery_pending
        except SyncError as exc:
            self.selection_label.setText(str(exc) if self.choices else "请选择来源；所选仓库范围包含其嵌套子仓库。")
        self.confirm_checkbox.setEnabled(executable)
        self.execute_button.setEnabled(executable and self.confirm_checkbox.isChecked())
        self.cancel_button.setVisible(self._task_active)
