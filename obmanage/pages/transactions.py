"""Dedicated host for existing recovery controllers and explicit record disposal."""
from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QPushButton

from .common import FeaturePage
from .controls import DropDownCombo
from ..management.deployment import DeploymentEngine, TERMINAL_BATCH_STATUSES
from ..management.trash import TrashCleanupEngine


class TransactionsPage(FeaturePage):
    page_key = "transactions"

    def __init__(self, state_dir: Path, distribution, trash):
        super().__init__("事务处理", "检查并处理待恢复事务；已结束事务可清除副本和记录，当前仓库保持不变。")
        self.state_dir = state_dir
        self.owners = (distribution, trash)
        self.distribution = distribution
        self._delegate = None
        # Reuse the tested controllers and the same global worker slot. Widgets
        # have exactly one visible home; deployment/cleanup pages stay focused.
        distribution.recovery_detached = True
        trash.recovery_detached = True
        for owner, frame in ((distribution, distribution.recovery_frame),
                             (trash, trash.legacy_recovery_panel)):
            owner.body.removeWidget(frame)
            self.body.addWidget(frame)
        distribution.recovery_frame.show()
        self.clear_mode = DropDownCombo()
        self.clear_mode.addItem("清除副本和记录", "all")
        self.clear_mode.addItem("仅删除记录，副本保留原位（不释放副本空间）", "record")
        distribution.recovery_frame.layout().addWidget(self.clear_mode)
        self.clear_mode.currentIndexChanged.connect(self._mode_changed)
        self.clear_confirm = QCheckBox("永久删除全部副本（含回退保全内容）及记录，不可恢复")
        self.clear_button = QPushButton("清除副本和记录")
        self.clear_button.setObjectName("Danger")
        distribution.recovery_frame.layout().addWidget(self.clear_confirm)
        distribution.recovery_frame.layout().addWidget(self.clear_button)
        self.legacy_confirm = QCheckBox("删除所选已清理的旧版隔离记录，不可恢复")
        self.legacy_clear = QPushButton("删除旧版记录")
        trash.legacy_recovery_panel.layout().addWidget(self.legacy_confirm)
        trash.legacy_recovery_panel.layout().addWidget(self.legacy_clear)
        self.legacy_confirm.toggled.connect(self.refresh_actions)
        self.legacy_clear.clicked.connect(self._clear_legacy)
        trash.operation_selector.currentIndexChanged.connect(self._reset_clear)
        self.refresh_button = QPushButton("刷新事务列表")
        self.body.addWidget(self.refresh_button)
        self.body.addStretch()
        self.clear_confirm.toggled.connect(self.refresh_actions)
        distribution.batch_selector.currentIndexChanged.connect(self._reset_clear)
        self.clear_button.clicked.connect(self._clear)
        self.refresh_button.clicked.connect(self.refresh_records)
        self.refresh_records()

    def _mode_changed(self, *_):
        record_only = self.clear_mode.currentData() == "record"
        self.clear_confirm.setText("已保存副本位置；只删除记录，副本今后由我手动管理" if record_only else
                                   "永久删除全部副本（含回退保全内容）及记录，不可恢复")
        self.clear_button.setText("仅删除记录" if record_only else "清除副本和记录")
        self._reset_clear()

    def _reset_clear(self, *_):
        self.clear_confirm.setChecked(False)
        self.legacy_confirm.setChecked(False)
        self.refresh_actions()

    def refresh_records(self):
        if self._task_active:
            return
        self._reset_clear()
        self.distribution._reload_persistent_batches()
        self.owners[1]._reload_operations()
        for owner in self.owners:
            owner.refresh_actions()
        self.refresh_actions()

    def invalidate_confirmation(self):
        self._reset_clear()
        for owner in self.owners:
            owner.invalidate_confirmation()

    def adopt_task(self, owner):
        self._delegate = owner
        self._task_kind = owner._task_kind
        self._task_active = True
        self._reset_clear()
        self.set_status("正在处理…", "busy")

    def task_cancellable(self):
        return self._delegate.task_cancellable() if self._delegate is not None else False

    def task_finished(self, status, payload):
        owner = self._delegate
        kind = self._task_kind
        super().task_finished(status, payload)
        self._delegate = None
        if owner is not None:
            owner.task_finished(status, payload)
            self.set_status(owner.status_label.text(), owner._status_kind)
        elif status == "ok" and kind == "删除旧版记录":
            self.set_status("所选旧版记录已删除。", "success")
        elif status == "ok":
            message = ("记录已删除；副本仍在原位置，由你手动管理。" if kind == "仅删除记录" else
                       "所选事务的副本和记录已清除。")
            self.set_status(message if payload.success else
                            "清除未完成，记录保留供重试：" + "；".join(payload.errors),
                            "success" if payload.success else "error")
            self.message_logged.emit(self.status_label.text())
        if kind != "inspect_recovery":
            self.refresh_records()
        self.refresh_actions()

    def _clear(self):
        owner = self.distribution
        preview = owner.recovery_preview
        if not self.clear_button.isEnabled() or not self.clear_confirm.isChecked() or preview is None:
            return
        record_only = self.clear_mode.currentData() == "record"
        if record_only:
            self.message_logged.emit("仅删除事务记录，保留副本位置：\n" + "\n\n".join(preview.details))
            self._reset_clear()
            self.start_task("仅删除记录", lambda _cancel, _progress:
                            DeploymentEngine(self.state_dir).forget_transaction(
                                preview.batch_id, expected_revision=preview.revision))
            return
        self._reset_clear()
        self.start_task("清除事务", lambda _cancel, _progress:
                        DeploymentEngine(self.state_dir).clear_transaction(preview.batch_id, preview=preview))

    def _clear_legacy(self):
        operation = self.owners[1].current_operation
        if not self.legacy_clear.isEnabled() or not self.legacy_confirm.isChecked() or operation is None:
            return
        self._reset_clear()
        self.start_task("删除旧版记录", lambda _cancel, _progress:
                        TrashCleanupEngine(self.state_dir).clear_record(operation.operation_id))

    def refresh_actions(self):
        if not hasattr(self, "clear_button"):
            return
        owner = self.distribution
        preview = owner.recovery_preview
        available = not self._global_busy and not self._task_active
        ready = (available and owner.current_batch is not None
                 and owner.current_batch.status in TERMINAL_BATCH_STATUSES
                 and preview is not None
                 and (self.clear_mode.currentData() == "record" or not preview.cleanup_error))
        self.clear_mode.setEnabled(available)
        self.clear_confirm.setEnabled(ready)
        self.clear_button.setEnabled(ready and self.clear_confirm.isChecked())
        self.refresh_button.setEnabled(available)

        legacy = self.owners[1].current_operation
        legacy_ready = (available and legacy is not None
                        and all(record.status == "finalized" for record in legacy.records))
        self.legacy_confirm.setEnabled(legacy_ready)
        self.legacy_clear.setEnabled(legacy_ready and self.legacy_confirm.isChecked())
