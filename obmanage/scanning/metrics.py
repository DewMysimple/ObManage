"""Optional aggregate diagnostics. No paths or file names leave this module."""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

_CURRENT: ContextVar[ScanMetrics | None] = ContextVar("scan_metrics", default=None)
_LOG = logging.getLogger("obmanage.scan")


@dataclass
class ScanMetrics:
    task: str = "scan"
    counts: Counter = field(default_factory=Counter)
    seconds: Counter = field(default_factory=Counter)
    status: str = "running"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counts[name] += amount

    def elapsed(self, name: str, amount: float) -> None:
        with self._lock:
            self.seconds[name] += amount

    def report(self) -> dict:
        with self._lock:
            return dict(task=self.task, status=self.status,
                        counts=dict(self.counts), seconds=dict(self.seconds))

    @contextmanager
    def activate(self):
        token = _CURRENT.set(self)
        started = time.perf_counter()
        try:
            yield self
            if self.status == "running":
                self.status = "success"
        except BaseException as exc:
            self.status = "cancelled" if type(exc).__name__ == "SyncCancelled" else "failed"
            raise
        finally:
            self.elapsed("total", time.perf_counter() - started)
            _CURRENT.reset(token)


def current() -> ScanMetrics | None:
    return _CURRENT.get()


def count(name: str, amount: int = 1) -> None:
    metrics = _CURRENT.get()
    if metrics is not None:
        metrics.add(name, amount)


def outcome(status: str) -> None:
    metrics = _CURRENT.get()
    if metrics is not None:
        metrics.status = status


@contextmanager
def phase(name: str):
    metrics = _CURRENT.get()
    if metrics is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        metrics.elapsed(name, time.perf_counter() - started)


def measured(name: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with phase(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def scan_task(name: str):
    """Nested calls share the task's metrics; debug logging enables collection."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            def execute():
                result = function(*args, **kwargs)
                if getattr(result, "errors", ()) or getattr(result, "issues", ()):
                    outcome("incomplete")
                return result
            if current() is not None:
                return execute()
            if not _LOG.isEnabledFor(logging.DEBUG):
                return function(*args, **kwargs)
            metrics = ScanMetrics(name)
            try:
                with metrics.activate():
                    return execute()
            finally:
                _LOG.debug("scan %s", metrics.report())
        return wrapped
    return decorate
