"""State-specific recovery actions with automatic inspection and one confirmation."""
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton

from .common import FeaturePage, format_bytes
from ..management.deployment import DeploymentEngine, TERMINAL_BATCH_STATUSES
from ..management.trash import TrashCleanupEngine


class TransactionsPage(FeaturePage):
    page_key = "transactions"

    def __init__(self, state_dir: Path, distribution, trash):
        super().__init__("事务处理", "选择事务后自动检查；保留现状、回退或清除历史副本。")
        self.state_dir = state_dir
        self.owners = (distribution, trash)
        self.distribution = distribution
        self._delegate = None
        self._confirmation_revision = 0
        self._inspection_pending = False
        self._inspection_status = None
        self._inspect_timer = QTimer(self)
        self._inspect_timer.setSingleShot(True)
        self._inspect_timer.timeout.connect(self._inspect_selected)
        for owner, frame in ((distribution, distribution.recovery_frame),
                             (trash, trash.legacy_recovery_panel)):
            owner.recovery_detached = True
            owner.body.removeWidget(frame)
            self.body.addWidget(frame)
        distribution.recovery_details_expanded = False
        distribution.resolve_button.clicked.disconnect(distribution._start_resolve)
        distribution.resolve_button.clicked.connect(self._resolve)
        distribution.rollback_button.clicked.disconnect(distribution._start_rollback)
        distribution.rollback_button.clicked.connect(self._rollback)
        layout = distribution.recovery_frame.layout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.insertWidget(2, self.summary)
        self.details_button = QPushButton("查看路径与副本")
        self.details_button.setCheckable(True)
        self.details_button.setObjectName("TextButton")
        self.details_button.toggled.connect(self._toggle_details)
        layout.insertWidget(3, self.details_button)
        actions = QHBoxLayout()
        layout.removeWidget(distribution.resolve_button)
        for index in range(layout.count()):
            row = layout.itemAt(index).layout()
            if row is not None:
                row.removeWidget(distribution.rollback_button)
        actions.addWidget(distribution.resolve_button)
        actions.addWidget(distribution.rollback_button)
        self.clear_button = QPushButton("清除副本和记录")
        self.clear_button.setObjectName("Danger")
        self.forget_button = QPushButton("仅删除记录")
        self.forget_button.setToolTip("保留全部副本，不释放副本空间；删除记录后需手动管理副本。")
        actions.addWidget(self.clear_button)
        actions.addWidget(self.forget_button)
        layout.addLayout(actions)
        self.clear_button.clicked.connect(lambda: self._clear(record_only=False))
        self.forget_button.clicked.connect(lambda: self._clear(record_only=True))
        distribution.batch_selector.currentIndexChanged.connect(self._selection_changed)

        # Finalizing an old quarantine also removes its finished record.
        trash.finalize_button.clicked.disconnect(trash._start_finalize)
        trash.finalize_button.clicked.connect(self._clear_legacy)
        self.legacy_clear = QPushButton("删除旧版记录")
        trash.legacy_recovery_panel.layout().addWidget(self.legacy_clear)
        self.legacy_clear.clicked.connect(self._clear_legacy)
        trash.operation_selector.currentIndexChanged.connect(self._legacy_selection_changed)
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

    def _toggle_details(self, expanded):
        self.distribution.recovery_details_expanded = expanded
        self.details_button.setText("收起路径与副本" if expanded else "查看路径与副本")
        self.distribution.refresh_actions()

    def _selection_changed(self, *_):
        self._confirmation_revision += 1
        self.details_button.setChecked(False)
        self._inspection_pending = True
        self.refresh_actions()
        self._inspect_timer.start(0)

    def _legacy_selection_changed(self, *_):
        self._confirmation_revision += 1
        self.refresh_actions()

    def _inspect_selected(self):
        if (not self._inspection_pending or not self.isVisible()
                or self._global_busy or self._task_active):
            return
        self._inspection_pending = False
        if self.distribution.current_batch is not None:
            self.distribution._start_inspect_recovery()

    def set_global_busy(self, busy):
        super().set_global_busy(busy)
        if not busy and self._inspection_pending:
            self._inspect_timer.start(0)

    def refresh_records(self):
        if self._task_active:
            return
        self.distribution._reload_persistent_batches()
        self.owners[1]._reload_operations()
        for owner in self.owners:
            owner.refresh_actions()
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
                else self._task_active and self._task_kind == "清除旧版事务")

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
            if kind == "清除旧版事务" and payload is not None and payload.status != "success":
                self.owners[1]._accept_operation_result("finalize", payload)
                self.set_status(self.owners[1].status_label.text(), self.owners[1]._status_kind)
            elif kind in {"清除旧版事务", "删除旧版记录"}:
                self.set_status("所选旧版事务已清除。", "success")
            else:
                message = ("记录已删除；副本仍在原位置，由你手动管理。" if kind == "仅删除记录" else
                           "所选事务的副本和记录已清除。")
                self.set_status(message if payload.success else
                                "清除未完成，记录保留供重试：" + "；".join(payload.errors),
                                "success" if payload.success else "error")
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
        # Qt's built-in details toggle has no Chinese translation by default.
        for button in dialog.buttons():
            if dialog.buttonRole(button) == QMessageBox.ButtonRole.ActionRole:
                button.setText("查看路径")
                button.setCheckable(True)
                button.clicked.connect(lambda checked, toggle=button:
                                       toggle.setText("收起路径" if checked else "查看路径"))
        return dialog.exec() == QMessageBox.StandardButton.Ok

    def _resolve(self):
        # This explicit action keeps all user data and copies.
        if self.distribution.resolve_button.isEnabled():
            self.distribution._start_resolve(confirmed=True)

    def _rollback(self):
        owner = self.distribution
        preview = owner.recovery_preview
        if not owner.rollback_button.isEnabled() or preview is None:
            return
        if not self._confirm("保全内容后回退", "将恢复所选事务涉及的子树，当前内容先保全为副本。\n"
                             "请关闭目标 Obsidian 仓库。", "\n\n".join(preview.details)):
            return
        if owner.recovery_preview is preview and owner.rollback_button.isEnabled():
            owner._start_rollback(confirmed=True)

    def _clear(self, *, record_only):
        owner = self.distribution
        preview = owner.recovery_preview
        button = self.forget_button if record_only else self.clear_button
        if not button.isEnabled() or preview is None:
            return
        action = "仅删除记录" if record_only else "清除副本和记录"
        message = ("只删除所选事务记录，全部副本保留原位。\n"
                   "今后需按详情中的路径手动管理副本，不释放副本空间。" if record_only else
                   f"永久删除所选事务的全部副本（含回退保全内容）及记录。\n{self._copy_summary(preview)}\n"
                   "当前仓库保持不变；删除后不可恢复。")
        if not self._confirm(action, message, "\n\n".join(preview.details)):
            return
        # Nested dialog events can deliver navigation or state updates.
        if owner.recovery_preview is not preview or not button.isEnabled():
            return
        if record_only:
            self.message_logged.emit("仅删除事务记录，保留副本位置：\n" + "\n\n".join(preview.details))
        owner._reset_recovery_preview()
        self.start_task(action, lambda _cancel, _progress:
                        DeploymentEngine(self.state_dir).forget_transaction(
                            preview.batch_id, expected_revision=preview.revision) if record_only else
                        DeploymentEngine(self.state_dir).clear_transaction(preview.batch_id, preview=preview))

    def _clear_legacy(self):
        owner = self.owners[1]
        operation = owner.current_operation
        if operation is None or not (self.legacy_clear.isEnabled() or owner.finalize_button.isEnabled()):
            return
        pending = owner._selected_operation_pending()
        confirmation_revision = self._confirmation_revision
        action = "清除旧版事务" if pending else "删除旧版记录"
        details = "\n\n".join(f"仓库：{r.vault_root}\n隔离副本：{r.backup_path}\n"
                               f"记录数量：{r.file_count} 个文件、{r.dir_count} 个目录"
                               for r in operation.records)
        message = ("永久删除所选旧版隔离副本及记录，删除后不可恢复。\n"
                   f"原记录共 {sum(r.file_count for r in operation.records)} 个文件、"
                   f"{sum(r.dir_count for r in operation.records)} 个目录，"
                   f"{format_bytes(sum(r.total_bytes for r in operation.records))}。\n"
                   "当前仓库及 .trash 保持不变。"
                   if pending else "所选批次的隔离副本已清理，只删除这条旧版记录。")
        if not self._confirm(action, message, details):
            return
        if (confirmation_revision != self._confirmation_revision
                or owner.current_operation is not operation or self._global_busy or self._task_active):
            return

        def clear(cancel, _progress):
            engine = TrashCleanupEngine(self.state_dir)
            if pending:
                result = engine.finalize(operation.operation_id, cancel=cancel)
                if result.status != "success":
                    return result
            engine.clear_record(operation.operation_id)
            return None

        self.start_task(action, clear)

    @staticmethod
    def _copy_summary(preview):
        trees = preview.cleanup_trees
        files = sum(len(tree.files) for tree in trees)
        directories = sum(len(tree.entries) - len(tree.files) + 1 for tree in trees)
        return (f"{len(trees)} 处副本 · {files} 个文件 · {directories} 个目录 · "
                f"{format_bytes(sum(tree.total_bytes for tree in trees))}")

    def refresh_actions(self):
        if not hasattr(self, "refresh_button"):
            return
        owner = self.distribution
        preview, batch = owner.recovery_preview, owner.current_batch
        available = not self._global_busy and not self._task_active
        ended = batch is not None and batch.status in TERMINAL_BATCH_STATUSES
        inspected = (preview is not None and batch is not None
                     and preview.batch_id == batch.batch_id and preview.revision == batch.revision)
        self.clear_button.setVisible(ended)
        self.forget_button.setVisible(ended)
        self.clear_button.setEnabled(available and ended and inspected and not preview.cleanup_error)
        self.forget_button.setEnabled(available and ended and inspected)
        self.clear_button.setToolTip(preview.cleanup_error if inspected else "自动检查完成后可清除")
        self.details_button.setVisible(inspected)
        self.details_button.setEnabled(available)
        if batch is not None and not inspected:
            self.summary.setText("正在自动检查…" if self._inspection_pending or self._task_active else
                                 "检查未完成，点击刷新重试。")
        elif inspected:
            text = self._copy_summary(preview)
            text = (("事务已结束，可清除历史副本；当前仓库保持不变。\n" if ended else
                     "保留现状会结束本事务并保留全部副本，不代表同步完整成功；也可保全后回退。\n") + text)
            if preview.cleanup_error:
                text += "\n副本无法验证，暂不可清除；可查看详情。"
            if not ended and preview.rollback_error:
                text += "\n暂不可回退：" + preview.rollback_error
            self.summary.setText(text)
        owner.batch_status.setVisible(owner._recovery_blocked)
        owner.recovery_frame.setVisible(batch is not None or owner._recovery_blocked)
        self.refresh_button.setEnabled(available)
        self.progress_bar.setVisible(self._task_active)

        legacy = self.owners[1]
        operation = legacy.current_operation
        pending = operation is not None and legacy._selected_operation_pending()
        legacy.finalize_confirm.hide()
        legacy.restore_button.setVisible(pending)
        legacy.finalize_button.setVisible(pending)
        legacy.finalize_button.setText("清除隔离副本和记录")
        legacy.finalize_button.setEnabled(available and pending and not legacy._recovery_blocked)
        legacy.operation_status.setVisible(legacy._recovery_blocked)
        self.legacy_clear.setVisible(operation is not None and not pending)
        self.legacy_clear.setEnabled(available and operation is not None and not pending)
        self.empty_label.setVisible(batch is None and operation is None
                                    and not owner._recovery_blocked and not legacy._recovery_blocked)
