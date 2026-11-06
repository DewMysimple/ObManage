"""Optional aggregate diagnostics. No paths or file names leave this module."""
from __future__ import annotations

import heapq
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
    _slow_vaults: list = field(default_factory=list, repr=False)

    def add(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counts[name] += amount

    def elapsed(self, name: str, amount: float) -> None:
        with self._lock:
            self.seconds[name] += amount

    def query(self, name, function, *args, **kwargs):
        """Measure one metadata API request, regardless of its caller."""
        started = time.perf_counter()
        failed = False
        try:
            return function(*args, **kwargs)
        except OSError:
            failed = True
            raise
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.counts["metadata_queries"] += 1
                self.counts[f"{name}_calls"] += 1
                if failed:
                    self.counts[f"{name}_errors"] += 1
                self.seconds[name] += elapsed

    def tally(self, names):
        with self._lock:
            return {name: self.counts[name] for name in names}

    def record_vault(self, ordinal, seconds, files, directories, size, complete, *, stage="statistics"):
        """Retain bounded, anonymous per-vault measurements for this task."""
        with self._lock:
            entry = (seconds, ordinal, files, directories, size, complete, stage)
            if len(self._slow_vaults) < 5:
                heapq.heappush(self._slow_vaults, entry)
            else:
                heapq.heappushpop(self._slow_vaults, entry)

    def report(self) -> dict:
        with self._lock:
            report = dict(task=self.task, status=self.status,
                          counts=dict(self.counts), seconds=dict(self.seconds))
            if self._slow_vaults:
                report["slow_vaults"] = [
                    dict(ordinal=ordinal, seconds=seconds, files=files,
                         directories=dirs, bytes=size, complete=complete, stage=stage)
                    for seconds, ordinal, files, dirs, size, complete, stage
                    in sorted(self._slow_vaults, reverse=True)
                ]
            return report

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


def debug_metrics(name):
    return ScanMetrics(name) if _LOG.isEnabledFor(logging.DEBUG) else None


def log_metrics(metrics):
    _LOG.debug("scan %s", metrics.report())


def count(name: str, amount: int = 1) -> None:
    metrics = _CURRENT.get()
    if metrics is not None:
        metrics.add(name, amount)


def outcome(status: str) -> None:
    metrics = _CURRENT.get()
    if metrics is not None:
        metrics.status = status


@contextmanager
def phase(name: str, *, tallies=()):
    metrics = _CURRENT.get()
    if metrics is None:
        yield
        return
    started = time.perf_counter()
    before = metrics.tally(tallies) if tallies else {}
    try:
        yield
    except Exception:
        metrics.add(f"{name}_exceptions")
        raise
    finally:
        metrics.elapsed(name, time.perf_counter() - started)
        if before:
            for counter, amount in metrics.tally(tallies).items():
                metrics.add(f"{name}_{counter}", amount - before[counter])


def measured(name: str, *, calls=False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            metrics = _CURRENT.get()
            if metrics is None:
                return function(*args, **kwargs)
            if calls:
                metrics.add(f"{name}_calls")
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
            metrics = debug_metrics(name)
            if metrics is None:
                return function(*args, **kwargs)
            try:
                with metrics.activate():
                    return execute()
            finally:
                log_metrics(metrics)
        return wrapped
    return decorate
