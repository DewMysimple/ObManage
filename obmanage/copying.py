"""Low-overhead streaming primitives shared by every verified copy path."""
from __future__ import annotations

import hashlib
from typing import BinaryIO, Callable


COPY_BUFFER_SIZE = 4 * 1024 * 1024


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
    buffer_size = min(COPY_BUFFER_SIZE, max(1, int(expected_size)))
    buffer = bytearray(buffer_size)
    view = memoryview(buffer)
    digest = hashlib.sha256()
    copied = 0
    while True:
        check_cancel()
        count = source.readinto(buffer)
        if not count:
            break
        block = view[:count]
        offset = 0
        while offset < count:
            check_cancel()
            written = destination.write(block[offset:])
            if written is None:
                written = count - offset
            if written <= 0:
                raise OSError("写入流未接受任何数据。")
            offset += written
        digest.update(block)
        copied += count
        if progress is not None:
            progress(copied)
    return copied, digest.hexdigest()


__all__ = ["COPY_BUFFER_SIZE", "copy_stream_and_hash"]
