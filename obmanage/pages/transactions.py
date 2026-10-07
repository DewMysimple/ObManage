"""One transaction list, state-specific recovery, and combined copy/record deletion."""
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QTimer, Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton

from .common import FeaturePage, format_bytes, panel
from .controls import DropDownCombo
from ..management.deployment import DeploymentEngine, TERMINAL_BATCH_STATUSES
from ..management.trash import TrashCleanupEngine, TrashOperationResult


class TransactionsPage(FeaturePage):
    page_key = "transactions"

    def __init__(self, state_dir: Path, distribution, trash):
        super().__init__("事务处理", "选择事务后自动检查；保留现状、回退或清除副本和记录。")
        self.state_dir = state_dir
        self.owners = (distribution, trash)
        self.distribution = distribution
        self._delegate = None
        self._confirmation_revision = 0
        self._inspection_pending = False
        self._inspection_status = None
        self._selecting = False
        self._inspect_timer = QTimer(self)
        self._inspect_timer.setSingleShot(True)
        self._inspect_timer.timeout.connect(self._inspect_selected)
        for owner, frame in ((distribution, distribution.recovery_frame),
                             (trash, trash.legacy_recovery_panel)):
            owner.recovery_detached = True
            frame.hide()
        trash.finalize_button.hide()
        trash.finalize_confirm.hide()

        self.transaction_frame, layout = panel(self.body)
        header = QHBoxLayout()
        title = QLabel("事务")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        self.selector = DropDownCombo()
        self.selector.setObjectName("transaction_selector")
        self.selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.selector.setMinimumContentsLength(18)
        self.selector.setMaxVisibleItems(12)
        header.addWidget(self.selector, 1)
        layout.addLayout(header)
        self.errors_label = QLabel()
        self.errors_label.setWordWrap(True)
        self.errors_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.errors_label)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.details_button = QPushButton("查看路径与副本")
        self.details_button.setCheckable(True)
        self.details_button.setObjectName("TextButton")
        self.details_button.toggled.connect(self._toggle_details)
        layout.addWidget(self.details_button)
        self.details = distribution.recovery_details
        layout.addWidget(self.details)
        distribution.recovery_details_expanded = False

        actions = QHBoxLayout()
        for button in (distribution.resolve_button, distribution.rollback_button, trash.restore_button):
            actions.addWidget(button)
        self.clear_button = QPushButton("清除副本和记录")
        self.clear_button.setObjectName("Danger")
        actions.addWidget(self.clear_button)
        layout.addLayout(actions)
        distribution.resolve_button.clicked.disconnect(distribution._start_resolve)
        distribution.resolve_button.clicked.connect(self._resolve)
        distribution.rollback_button.clicked.disconnect(distribution._start_rollback)
        distribution.rollback_button.clicked.connect(self._rollback)
        self.clear_button.clicked.connect(self._clear)
        self.selector.currentIndexChanged.connect(self._selection_changed)
        distribution.batch_selector.currentIndexChanged.connect(
            lambda *_: self._controller_selection_changed("deploy", distribution.current_batch))
        trash.operation_selector.currentIndexChanged.connect(
            lambda *_: self._controller_selection_changed("trash", trash.current_operation))

        self.empty_label = QLabel("没有需要处理的事务。")
        self.empty_label.setObjectName("Muted")
        self.body.addWidget(self.empty_label)
        self.refresh_button = QPushButton("刷新事务列表")
        self.body.addWidget(self.refresh_button)
        self.body.addStretch()
        self.refresh_button.clicked.connect(self.refresh_records)
        self.refresh_records()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_records()

    def _selected_kind(self):
        return (self.selector.currentData() or "").partition(":")[0]

    def _controller_selection_changed(self, kind, record):
        if self._selecting or record is None:
            return
        identifier = record.batch_id if kind == "deploy" else record.operation_id
        self.selector.setCurrentIndex(self.selector.findData(f"{kind}:{identifier}"))

    def _toggle_details(self, expanded):
        self.distribution.recovery_details_expanded = expanded
        self.details_button.setText("收起路径与副本" if expanded else "查看路径与副本")
        self.refresh_actions()

    def _selection_changed(self, *_):
        self._confirmation_revision += 1
        self._selecting = True
        try:
            self.details_button.setChecked(False)
            self.distribution._reset_recovery_preview()
            kind, _, identifier = (self.selector.currentData() or "").partition(":")
            owner = self.distribution if kind == "deploy" else self.owners[1]
            selector = owner.batch_selector if kind == "deploy" else owner.operation_selector
            selector.setCurrentIndex(selector.findData(identifier))
            for controller in self.owners:
                controller.refresh_actions()
            self._inspection_pending = kind == "deploy"
            if kind == "trash" and owner.current_operation is not None:
                self.details.setPlainText(self._trash_details(owner.current_operation))
        finally:
            self._selecting = False
        self.refresh_actions()
        self._inspect_timer.start(0)

    def _inspect_selected(self):
        if (not self._inspection_pending or not self.isVisible()
                or self._global_busy or self._task_active):
            return
        self._inspection_pending = False
        if self._selected_kind() == "deploy" and self.distribution.current_batch is not None:
            self.distribution._start_inspect_recovery()

    def set_global_busy(self, busy):
        super().set_global_busy(busy)
        if not busy and self._inspection_pending:
            self._inspect_timer.start(0)

    def refresh_records(self):
        if self._task_active:
            return
        selected = self.selector.currentData()
        self._selecting = True
        try:
            self.distribution._reload_persistent_batches()
            self.owners[1]._reload_operations()
            choices = []
            for batch in self.distribution._batches:
                choices.append((batch.status in TERMINAL_BATCH_STATUSES, -batch.updated_at,
                                f"deploy:{batch.batch_id}", self.distribution._batch_choice_text(batch),
                                self.distribution._batch_tooltip(batch)))
            for operation in self.owners[1]._operations:
                choices.append((not self.owners[1]._operation_pending(operation), -operation.created_at,
                                f"trash:{operation.operation_id}", self.owners[1]._operation_choice_text(operation),
                                self._trash_details(operation)))
            blocker = QSignalBlocker(self.selector)
            self.selector.clear()
            for _, _, key, label, tooltip in sorted(choices):
                self.selector.addItem(label, key)
                self.selector.setItemData(self.selector.count() - 1, tooltip, Qt.ItemDataRole.ToolTipRole)
            index = self.selector.findData(selected) if selected else -1
            self.selector.setCurrentIndex(index if index >= 0 else 0)
            del blocker
        finally:
            self._selecting = False
        self._selection_changed()

    def invalidate_confirmation(self):
        self._confirmation_revision += 1
        self._inspection_pending = False
        self._inspect_timer.stop()
        self.details_button.setChecked(False)
        for owner in self.owners:
            owner.invalidate_confirmation()
        self.refresh_actions()

    def adopt_task(self, owner):
        self._delegate = owner
        self._task_kind = owner._task_kind
        self._task_active = True
        if self._task_kind == "inspect_recovery":
            self._inspection_status = (self.status_label.text(), self._status_kind)
        self.set_status("正在检查事务…" if self._task_kind == "inspect_recovery" else "正在处理…", "busy")

    def task_cancellable(self):
        return (self._delegate.task_cancellable() if self._delegate is not None
                else self._task_active and self._task_kind == "clear_trash")

    def task_finished(self, status, payload):
        owner, kind = self._delegate, self._task_kind
        super().task_finished(status, payload)
        self._delegate = None
        if owner is not None:
            owner.task_finished(status, payload)
            if kind == "inspect_recovery" and status == "ok":
                previous = self._inspection_status
                self.set_status(*(previous if previous and previous[1] in {"success", "error", "warning"}
                                  else ("检查完成，请选择操作。", "neutral")))
            else:
                self.set_status(owner.status_label.text(), owner._status_kind)
        elif status == "ok":
            if isinstance(payload, TrashOperationResult):
                success = payload.status == "success"
                errors = tuple(failure.message for failure in payload.failures)
                if payload.status == "cancelled":
                    errors = (*errors, "操作已取消")
            else:
                success, errors = payload.success, payload.errors
            self.set_status("所选事务的副本和记录已清除。" if success else
                            "清除未完成，记录保留供重试：" + "；".join(errors),
                            "success" if success else "error")
            self.message_logged.emit(self.status_label.text())
        self._task_kind = ""
        if kind != "inspect_recovery":
            self.refresh_records()
        self.refresh_actions()

    def _confirm(self, action, message, details):
        dialog = QMessageBox(QMessageBox.Icon.Warning, action, message,
                             QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setDetailedText(details)
        dialog.button(QMessageBox.StandardButton.Ok).setText(action)
        dialog.button(QMessageBox.StandardButton.Cancel).setText("取消")
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        for button in dialog.buttons():
            if dialog.buttonRole(button) == QMessageBox.ButtonRole.ActionRole:
                button.setText("查看路径")
                button.setCheckable(True)
                button.clicked.connect(lambda checked, toggle=button:
                                       toggle.setText("收起路径" if checked else "查看路径"))
        return dialog.exec() == QMessageBox.StandardButton.Ok

    def _resolve(self):
        if self._selected_kind() == "deploy" and self.distribution.resolve_button.isEnabled():
            self.distribution._start_resolve(confirmed=True)

    def _rollback(self):
        owner = self.distribution
        preview = owner.recovery_preview
        revision = self._confirmation_revision
        if self._selected_kind() != "deploy" or not owner.rollback_button.isEnabled() or preview is None:
            return
        if not self._confirm("保全内容后回退", "将恢复所选事务涉及的子树，当前内容先保全为副本。\n"
                             "请关闭目标 Obsidian 仓库。", "\n\n".join(preview.details)):
            return
        if (revision == self._confirmation_revision and owner.recovery_preview is preview
                and owner.rollback_button.isEnabled()):
            owner._start_rollback(confirmed=True)

    def _clear(self):
        if not self.clear_button.isEnabled():
            return
        revision = self._confirmation_revision
        kind = self._selected_kind()
        if kind == "deploy":
            preview = self.distribution.recovery_preview
            if preview is None:
                return
            summary, details = self._copy_summary(preview), "\n\n".join(preview.details)
        else:
            operation = self.owners[1].current_operation
            if operation is None:
                return
            summary, details = self._trash_summary(operation), self._trash_details(operation)
        if not self._confirm("清除副本和记录", "永久删除所选事务的全部副本及记录。\n"
                             f"{summary}\n当前仓库保持不变；删除后不可恢复。", details):
            return
        if revision != self._confirmation_revision or not self.clear_button.isEnabled():
            return
        if kind == "deploy":
            if self.distribution.recovery_preview is not preview:
                return
            self.distribution._reset_recovery_preview()
            self.start_task("clear_deploy", lambda _cancel, _progress:
                            DeploymentEngine(self.state_dir).clear_transaction(preview.batch_id, preview=preview))
        elif self.owners[1].current_operation is operation:
            self.start_task("clear_trash", lambda cancel, _progress:
                            TrashCleanupEngine(self.state_dir).clear_transaction(operation.operation_id, cancel=cancel))

    @staticmethod
    def _copy_summary(preview):
        trees = preview.cleanup_trees
        files = sum(len(tree.files) for tree in trees)
        directories = sum(len(tree.entries) - len(tree.files) + 1 for tree in trees)
        return (f"{len(trees)} 处副本 · {files} 个文件 · {directories} 个目录 · "
                f"{format_bytes(sum(tree.total_bytes for tree in trees))}")

    @staticmethod
    def _trash_summary(operation):
        records = tuple(r for r in operation.records if r.status != "finalized")
        return (f"{len(records)} 处副本 · 原清单 {sum(r.file_count for r in records)} 个文件 · "
                f"{sum(r.dir_count for r in records)} 个目录 · "
                f"{format_bytes(sum(r.total_bytes for r in records))}")

    @staticmethod
    def _trash_details(operation):
        return "\n\n".join(f"仓库：{r.vault_root}\n副本：{r.backup_path}\n"
                           f"原清单：{r.file_count} 个文件、{r.dir_count} 个目录"
                           for r in operation.records)

    def refresh_actions(self):
        if not hasattr(self, "refresh_button"):
            return
        owner, trash = self.owners
        kind = self._selected_kind()
        preview, batch = owner.recovery_preview, owner.current_batch
        operation = trash.current_operation if kind == "trash" else None
        available = not self._global_busy and not self._task_active
        ended = kind == "deploy" and batch is not None and batch.status in TERMINAL_BATCH_STATUSES
        inspected = (kind == "deploy" and preview is not None and batch is not None
                     and preview.batch_id == batch.batch_id and preview.revision == batch.revision)
        owner.resolve_button.setVisible(kind == "deploy" and not ended and inspected)
        owner.rollback_button.setVisible(kind == "deploy" and not ended and inspected)
        trash.restore_button.setVisible(operation is not None and trash._selected_operation_pending())
        self.clear_button.setVisible(ended or operation is not None)
        self.clear_button.setEnabled(available and (ended and inspected and not preview.cleanup_error
                                    or operation is not None and not trash._recovery_blocked))
        self.clear_button.setToolTip(preview.cleanup_error if inspected else "")
        self.details_button.setVisible(inspected or operation is not None)
        self.details_button.setEnabled(available)
        self.details.setVisible((inspected or operation is not None) and self.details_button.isChecked())
        if kind == "deploy" and batch is not None and not inspected:
            self.summary.setText("正在自动检查…" if self._inspection_pending or self._task_active else
                                 "检查未完成，点击刷新重试。")
        elif inspected:
            text = (("事务已结束，可清除副本和记录；当前仓库保持不变。\n" if ended else
                     "保留现状会结束本事务并保留全部副本，不代表同步完整成功；也可保全后回退。\n")
                    + self._copy_summary(preview))
            if preview.cleanup_error:
                text += "\n副本无法验证，暂不可清除；可查看详情。"
            if not ended and preview.rollback_error:
                text += "\n暂不可回退：" + preview.rollback_error
            self.summary.setText(text)
        elif operation is not None:
            self.summary.setText(("可恢复至空 .trash，或清除副本和记录。\n"
                                  if trash.restore_button.isEnabled() else
                                  "可清除副本和记录；当前仓库保持不变。\n") + self._trash_summary(operation))
        else:
            self.summary.clear()
        errors = [controller.batch_status.text() if controller is owner else controller.operation_status.text()
                  for controller in self.owners if controller._recovery_blocked]
        self.errors_label.setText("\n".join(errors))
        self.errors_label.setVisible(bool(errors))
        owner.recovery_frame.hide()
        trash.legacy_recovery_panel.hide()
        self.transaction_frame.setVisible(self.selector.count() > 0 or bool(errors))
        self.selector.setEnabled(available and self.selector.count() > 0)
        self.empty_label.setVisible(self.selector.count() == 0 and not errors)
        self.refresh_button.setEnabled(available)
        self.progress_bar.setVisible(self._task_active)
