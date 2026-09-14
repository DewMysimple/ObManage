from __future__ import annotations

import hashlib
import io

import pytest

from obmanage.copying import copy_stream_and_hash


class ShortWriter(io.BytesIO):
    def write(self, value):
        return super().write(value[:3])


def test_shared_copy_stream_completes_short_writes_and_hashes_source():
    payload = b"verified-copy-payload"
    source = io.BytesIO(payload)
    destination = ShortWriter()
    reports = []

    copied, digest = copy_stream_and_hash(
        source,
        destination,
        len(payload),
        check_cancel=lambda: None,
        progress=reports.append,
    )

    assert copied == len(payload)
    assert destination.getvalue() == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert reports == [len(payload)]


def test_shared_copy_stream_checks_cancellation_during_short_writes():
    checks = 0

    def cancel() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        copy_stream_and_hash(
            io.BytesIO(b"more-than-one-short-write"),
            ShortWriter(),
            25,
            check_cancel=cancel,
        )
