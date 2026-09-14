"""One-at-a-time background task plumbing shared by feature pages."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal, Slot

from .models import Progress, SyncCancelled


FeatureOperation = Callable[[threading.Event, Callable[[Progress], None]], Any]


class FeatureWorker(QThread):
    """Run a feature service off the GUI thread and retain QThread ownership rules."""

    progress = Signal(object)
    completed = Signal(object)

    def __init__(self, operation: FeatureOperation) -> None:
        super().__init__()
        self.operation = operation
        self.cancel_event = threading.Event()
        self._last_progress: float | None = None
        self._last_phase = ""

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def _progress(self, event: Progress) -> None:
        now = time.monotonic()
        terminal = event.phase in {"done", "error"}
        # Phase changes can occur several times per tiny copied file
        # (copy/verify/copied).  Treat the event stream as one visual timeline
        # and coalesce it by time; otherwise phase transitions bypass the
        # throttle and flood the GUI queue. Terminal state is never delayed.
        if terminal or self._last_progress is None or now - self._last_progress >= 0.10:
            self._last_progress = now
            self._last_phase = event.phase
            self.progress.emit(event)

    @Slot()
    def run(self) -> None:
        try:
            value = self.operation(self.cancel_event, self._progress)
            self.completed.emit(("ok", value))
        except SyncCancelled:
            self.completed.emit(("cancelled", None))
        except Exception as exc:
            self.completed.emit(("error", f"{type(exc).__name__}: {exc}"))
