"""Instrument real directory reads without retaining cached DirEntry metadata."""
from __future__ import annotations

import os
import time

from .metrics import current


def scandir(path):
    if current() is None:
        return os.scandir(path)
    return _MeasuredDirectory(path)


class _MeasuredDirectory:
    """os.scandir-compatible lifetime; timing excludes consumer processing."""

    def __init__(self, path):
        self.metrics = current()
        started = time.perf_counter() if self.metrics else 0
        try:
            self.context = os.scandir(path)
            self.iterator = None
        finally:
            if self.metrics:
                self.metrics.add("directory_enumerations")
                self.metrics.elapsed("enumeration", time.perf_counter() - started)

    def __enter__(self):
        self.iterator = iter(self.context.__enter__())
        return self

    def __exit__(self, *args):
        return self.context.__exit__(*args)

    def __iter__(self):
        return self

    def __next__(self):
        if self.iterator is None:
            self.iterator = iter(self.context)
        started = time.perf_counter() if self.metrics else 0
        try:
            entry = next(self.iterator)
        finally:
            if self.metrics:
                self.metrics.elapsed("enumeration", time.perf_counter() - started)
        if self.metrics:
            self.metrics.add("directory_entries")
        return entry

    def close(self):
        self.context.close()
