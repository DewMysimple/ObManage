"""Low-overhead streaming primitives shared by every verified copy path."""
from __future__ import annotations

import hashlib
import time
from typing import BinaryIO, Callable
from .reading import BUFFER_LIMIT, buffer_size
from .scanning.metrics import current


COPY_BUFFER_SIZE = BUFFER_LIMIT


def copy_stream_and_hash(
    source: BinaryIO,
    destination: BinaryIO,
    expected_size: int,
    *,
    check_cancel: Callable[[], None],
    progress: Callable[[int], None] | None = None,
) -> tuple[int, str]:
    """Copy a stream, returning its byte count and SHA-256 in one source pass.

    A size-aware reusable buffer avoids allocating a fresh large ``bytes`` object
    for every block while also avoiding a multi-megabyte allocation for each
    tiny Obsidian metadata file.  Short writes are completed explicitly.
    """
    buffer = bytearray(buffer_size(expected_size))
    view = memoryview(buffer)
    digest = hashlib.sha256()
    copied = 0
    metrics = current()
    while True:
        check_cancel()
        if metrics is None:
            count = source.readinto(buffer)
        else:
            started = time.perf_counter()
            count = source.readinto(buffer)
            metrics.elapsed("source_read", time.perf_counter() - started)
            metrics.add("source_read_bytes", count)
        if not count:
            break
        block = view[:count]
        offset = 0
        while offset < count:
            check_cancel()
            if metrics is None:
                written = destination.write(block[offset:])
            else:
                started = time.perf_counter()
                written = destination.write(block[offset:])
                metrics.elapsed("stage_write", time.perf_counter() - started)
            if written is None:
                written = count - offset
            if written <= 0:
                raise OSError("写入流未接受任何数据。")
            offset += written
            if metrics is not None:
                metrics.add("stage_write_bytes", written)
        digest.update(block)
        copied += count
        if progress is not None:
            progress(copied)
    return copied, digest.hexdigest()


__all__ = ["COPY_BUFFER_SIZE", "copy_stream_and_hash"]
