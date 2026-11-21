"""One-at-a-time background task plumbing shared by feature pages."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

from .models import Progress, SyncCancelled
from .scanning.metrics import current, scan_task, outcome


FeatureOperation = Callable[[threading.Event, Callable[[Progress], None]], Any]


@dataclass(frozen=True)
class TaskConfirmation:
    title: str
    summary: str
    details: str = ""
    approve_text: str = "确认同步"
    payload: Any = None


class PendingConfirmation:
    def __init__(self, prompt: TaskConfirmation) -> None:
        self.prompt = prompt
        self._answered = threading.Event()
        self._approved = False

    def resolve(self, approved: bool) -> None:
        if not self._answered.is_set():
            self._approved = approved
            self._answered.set()


class ConfirmationExchange:
    """Keep one worker and its cancellation context while the GUI decides."""

    def __init__(self, notify, cancel_event: threading.Event) -> None:
        self._notify = notify
        self._cancel = cancel_event

    def ask(self, prompt: TaskConfirmation) -> bool:
        if self._cancel.is_set():
            return False
        pending = PendingConfirmation(prompt)
        self._notify(pending)
        while not pending._answered.wait(0.05):
            if self._cancel.is_set():
                return False
        return pending._approved and not self._cancel.is_set()


@dataclass(frozen=True)
class ConfirmableOperation:
    run: Callable[[threading.Event, Callable[[Progress], None],
                   Callable[[TaskConfirmation], bool]], Any]


class ConfirmationDialog(QDialog):
    def __init__(self, prompt: TaskConfirmation, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(prompt.title)
        layout = QVBoxLayout(self)
        summary = QLabel(prompt.summary)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if prompt.details:
            details = QPlainTextEdit(prompt.details, readOnly=True)
            details.setAccessibleName("同步目标与范围")
            layout.addWidget(details)
        buttons = QDialogButtonBox()
        approve = buttons.addButton(prompt.approve_text, QDialogButtonBox.ButtonRole.AcceptRole)
        reject = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        approve.setAutoDefault(False)
        reject.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(620, 340 if prompt.details else 170)


class FeatureWorker(QThread):
    """Run a feature service off the GUI thread and retain QThread ownership rules."""

    progress = Signal(object)
    completed = Signal(object)
    confirmation_requested = Signal(object)

    def __init__(self, operation: FeatureOperation | ConfirmableOperation, *, cancellable=True) -> None:
        super().__init__()
        self.operation = operation
        self.cancel_event = threading.Event()
        self.confirmation_exchange = (
            ConfirmationExchange(self.confirmation_requested.emit, self.cancel_event)
            if isinstance(operation, ConfirmableOperation) else None
        )
        self.cancellable = cancellable
        self._cancel_requested_at = None
        self._last_progress: float | None = None
        self._last_phase = ""

    def request_cancel(self) -> None:
        if self._cancel_requested_at is None:
            self._cancel_requested_at = time.perf_counter()
        self.cancel_event.set()

    def _progress(self, event: Progress) -> None:
        now = time.monotonic()
        immediate = event.phase in {
            "done", "error", "closing-sessions", "final-analysis", "recovering-deployment", "restoring-sessions"
        }
        # Phase changes can occur several times per tiny copied file
        # (copy/verify/copied).  Treat the event stream as one visual timeline
        # and coalesce it by time; otherwise phase transitions bypass the
        # throttle and flood the GUI queue. Terminal state is never delayed.
        if immediate or self._last_progress is None or now - self._last_progress >= 0.10:
            self._last_progress = now
            self._last_phase = event.phase
            self.progress.emit(event)

    @Slot()
    @scan_task("management_task")
    def run(self) -> None:
        metrics = current()
        if metrics is not None:
            metrics.add("cancellable", int(self.cancellable))
        try:
            if self.confirmation_exchange is not None:
                value = self.operation.run(self.cancel_event, self._progress, self.confirmation_exchange.ask)
            else:
                value = self.operation(self.cancel_event, self._progress)
            self.completed.emit(("ok", value))
        except SyncCancelled:
            outcome("cancelled")
            self.completed.emit(("cancelled", None))
        except Exception as exc:
            outcome("failed")
            self.completed.emit(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            if metrics is not None and self._cancel_requested_at is not None:
                metrics.elapsed("cancel_latency", time.perf_counter() - self._cancel_requested_at)
