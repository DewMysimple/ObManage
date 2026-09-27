import hashlib
import io

import pytest

from obmanage import reading
from obmanage.models import SyncCancelled, SyncError


@pytest.mark.parametrize("size", [0, 1, 127, 5 * 1024 * 1024])
def test_hash_stream_is_bounded_and_reads_to_eof_even_after_expected_size(size):
    data = b"a" * size + b"changed-after-preview"
    allocations = []
    class ShortReader(io.BytesIO):
        def readinto(self, buffer):
            allocations.append(len(buffer))
            return super().readinto(memoryview(buffer)[:131071])
    count, digest = reading.hash_stream(ShortReader(data), size, check_cancel=lambda: None)
    assert count == len(data)
    assert digest == hashlib.sha256(data).hexdigest()
    assert max(allocations) == min(reading.BUFFER_LIMIT, max(1, size))


def test_hash_stream_observes_cancel_before_read():
    stream = io.BytesIO(b"never read")
    def cancel():
        raise SyncCancelled()
    with pytest.raises(SyncCancelled):
        reading.hash_stream(stream, 10, check_cancel=cancel)
    assert stream.tell() == 0


def test_read_scope_rechecks_ancestors_after_count_time_and_directory_change(tmp_path, monkeypatch):
    checks = []
    clock = [0.0]
    original = reading.assert_plain_chain
    monkeypatch.setattr(reading, "assert_plain_chain", lambda p: (checks.append(p), original(p)))
    monkeypatch.setattr(reading.time, "monotonic", lambda: clock[0])
    (tmp_path / "a").write_bytes(b"a")
    scope = reading.ReadScope(str(tmp_path))
    for _ in range(16):
        scope.child("a")
    assert len(checks) == 1
    scope.child("a")
    assert len(checks) == 2
    clock[0] = .26
    scope.child("a")
    assert len(checks) == 3
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"b")
    scope.child("sub/b")
    assert len(checks) == 4


@pytest.mark.parametrize("relative", ["../escape", "a/../../escape", "a//b", "missing"])
def test_read_scope_rejects_escape_and_missing(tmp_path, relative):
    with pytest.raises(SyncError):
        reading.ReadScope(str(tmp_path)).child(relative)


def test_read_scope_cannot_be_used_for_directory_or_link(tmp_path):
    (tmp_path / "dir").mkdir()
    scope = reading.ReadScope(str(tmp_path))
    with pytest.raises(SyncError):
        scope.child("dir")
    (tmp_path / "file").write_bytes(b"a")
    try:
        (tmp_path / "link").symlink_to(tmp_path / "file")
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(SyncError):
        scope.child("link")
