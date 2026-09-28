"""Bounded streaming shared by preview, verification, copying and statistics.

Only byte transport lives here. Callers retain their path/handle identity checks,
content epochs, final manifests and write authorization at every safety boundary.
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import BinaryIO, Callable

from .models import SyncError
from .paths import assert_plain_chain, lexical_child_path, snapshot

BUFFER_LIMIT = 4 * 1024 * 1024


class ReadScope:
    """Bound adjacent read-only ancestor checks; never use for a filesystem write.

    Every file still needs its own before/open-handle/after snapshot checks. The
    owning scan must finish with a full manifest/chain revalidation. A scope is
    local to one scan and never persists across previews or execution phases.
    """

    def __init__(self, root: str):
        self.root = root
        self._parent = None
        self._checked_at = 0.0
        self._remaining = 0

    def observe(self, relative: str) -> tuple[str, dict]:
        """Check a live ordinary child, sharing only bounded ancestor work."""
        path = lexical_child_path(self.root, relative)
        parent = os.path.dirname(path)
        now = time.monotonic()
        if parent != self._parent or self._remaining <= 0 or now - self._checked_at >= 0.25:
            assert_plain_chain(parent)
            self._parent, self._checked_at, self._remaining = parent, now, 16
        self._remaining -= 1
        state = snapshot(path)
        if state is None or state["kind"] not in ("file", "dir"):
            raise SyncError(f"读取路径已消失或变成链接、特殊项目：{path}")
        return path, state

    def child(self, relative: str) -> str:
        path, state = self.observe(relative)
        if state["kind"] != "file":
            raise SyncError(f"读取路径变成非普通文件：{path}")
        return path


def buffer_size(expected_size: int, limit: int = BUFFER_LIMIT) -> int:
    return min(limit, max(1, int(expected_size)))


def hash_stream(stream: BinaryIO, expected_size: int, *,
                check_cancel: Callable[[], None],
                progress: Callable[[int], None] | None = None) -> tuple[int, str]:
    """Read through EOF with bounded reusable storage, including short reads."""
    buffer = bytearray(buffer_size(expected_size))
    view = memoryview(buffer)
    digest = hashlib.sha256()
    completed = 0
    while True:
        check_cancel()
        count = stream.readinto(buffer)
        if not count:
            break
        digest.update(view[:count])
        completed += count
        if progress is not None:
            progress(completed)
    return completed, digest.hexdigest()
