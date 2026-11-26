"""Execution-only read-cache oplocks for a local Windows exFAT mirror source."""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes as w

from .paths import native
from .scanning.metrics import count


# Bound kernel handles and pending I/O. Uncovered directories use live scans.
_HANDLE_LIMIT = 32768
_PENDING = 0x103  # STATUS_PENDING, as tested by HasOverlappedIoCompleted.


class _Overlapped(ctypes.Structure):
    _fields_ = (("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                ("offset", w.DWORD), ("offset_high", w.DWORD), ("event", w.HANDLE))


class _OplockRequest(ctypes.Structure):
    _fields_ = (("version", w.WORD), ("length", w.WORD),
                ("level", w.DWORD), ("flags", w.DWORD))


class _OplockReply(ctypes.Structure):
    _fields_ = (("version", w.WORD), ("length", w.WORD), ("old_level", w.DWORD),
                ("new_level", w.DWORD), ("flags", w.DWORD), ("access", w.DWORD),
                ("share", w.WORD))


class _ReadOplock(ctypes.Structure):
    # The kernel owns these buffers until the overlapped request completes.
    _fields_ = (("io", _Overlapped), ("output", _OplockReply))

    @property
    def intact(self):
        return self.io.internal == _PENDING


class _WindowsReadCache:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        def bind(name, result, *arguments):
            function = getattr(kernel, name)
            function.argtypes = arguments
            function.restype = result
            return function

        self.create = bind("CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
                           w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
        self.control = bind("DeviceIoControl", w.BOOL, w.HANDLE, w.DWORD, w.LPVOID,
                            w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.LPVOID)
        self.release = bind("CloseHandle", w.BOOL, w.HANDLE)
        self.reset = bind("ResetEvent", w.BOOL, w.HANDLE)
        self.signal = bind("SetEvent", w.BOOL, w.HANDLE)
        self.wait = bind("WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
        self.notify = bind("FindFirstChangeNotificationW", w.HANDLE, w.LPCWSTR, w.BOOL, w.DWORD)
        self.close_notify = bind("FindCloseChangeNotification", w.BOOL, w.HANDLE)
        self.notification = None
        create_event = bind("CreateEventW", w.HANDLE, w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR)
        self.event = create_event(None, True, False, None)
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())
        # Read + handle caching lets the filesystem request that we surrender
        # an open handle when another program renames a directory or replaces a file.
        self.request = _OplockRequest(1, ctypes.sizeof(_OplockRequest), 3, 1)
        self.pending = []
        self.lock = threading.Lock()
        self.capturing = True
        self.closed = False
        self.disabled = False
        self.worker = threading.Thread(target=self._serve_breaks, name="obmanage-source-oplocks")
        try:
            self.worker.start()
        except RuntimeError as exc:
            self.release(self.event)
            raise OSError("Cannot start directory change tracking") from exc

    def _serve_breaks(self):
        while True:
            # Submitting another overlapped request may reset the shared event.
            # During capture also drain already-completed breaks on timeout.
            status = self.wait(self.event, 50 if self.capturing else 0xFFFFFFFF)
            with self.lock:
                if self.closed:
                    return
                if status not in (0, 258):
                    self.disabled = True
                self.reset(self.event)
                for handle, lease in self.pending:
                    if lease.handle and not lease.intact:
                        if lease.output.flags & 1:  # REQUEST_OPLOCK_OUTPUT_FLAG_ACK_REQUIRED
                            self.disabled = True
                        self.release(handle)
                        lease.handle = None
                if self.disabled:
                    # An ancestor rename can involve every open descendant.
                    # Hand them all back, then use live scans for the rest of this run.
                    self._release_all()
                    return

    def seal(self):
        with self.lock:
            self.capturing = False
            self.signal(self.event)

    def _release_all(self):
        for handle, lease in self.pending:
            if lease.handle:
                self.release(handle)
                lease.handle = None
        if self.notification is not None:
            self.close_notify(self.notification)
            self.notification = None

    def watch_tree(self, root):
        # exFAT can change just the creation timestamp without breaking either
        # read lease. Watch that filter and directory renames from before enumeration.
        handle = self.notify(native(root), True, 0x40 | 0x02)
        if handle == ctypes.c_void_p(-1).value:
            self.disabled = True
        else:
            self.notification = handle

    def tree_intact(self):
        with self.lock:
            return (not self.disabled and self.notification is not None
                    and self.wait(self.notification, 0) == 258)  # WAIT_TIMEOUT

    def watch(self, path):
        if self.disabled or len(self.pending) >= _HANDLE_LIMIT:
            return None
        # FILE_READ_ATTRIBUTES, share read/write/delete, OPEN_EXISTING,
        # OVERLAPPED | BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
        handle = self.create(native(path), 0x80, 7, None, 3, 0x42200000, None)
        if handle == ctypes.c_void_p(-1).value:
            return None
        lock = _ReadOplock()
        lock.io.event = self.event
        size = ctypes.sizeof(self.request)
        done = self.control(handle, 0x90240, ctypes.byref(self.request), size,
                            ctypes.byref(lock.output), ctypes.sizeof(_OplockReply), None,
                            ctypes.byref(lock.io))  # FSCTL_REQUEST_OPLOCK
        if done or ctypes.get_last_error() != 997:  # Only ERROR_IO_PENDING grants a lease.
            self.release(handle)
            return None
        with self.lock:
            lock.handle = handle
            self.pending.append((handle, lock))
            if self.disabled:
                self.release(handle)
                lock.handle = None
        return lock

    def close(self):
        # Closing the last handle releases its oplock. Keep every OVERLAPPED and
        # output buffer alive until completion, including on device removal.
        count("source_read_cache_handles", len(self.pending))
        with self.lock:
            self.closed = True
            self.signal(self.event)
        self.worker.join()
        self._release_all()
        remaining = [lock for _, lock in self.pending if lock.intact]
        while remaining:
            self.reset(self.event)
            remaining = [lock for lock in remaining if lock.intact]
            if remaining:
                self.wait(self.event, 0xFFFFFFFF)
        self.pending.clear()
        self.release(self.event)


class SourceRecheck:
    """Track membership per directory and in-flight data changes per skipped file."""

    def __init__(self, cache, skipped):
        self.cache = cache
        self.skipped = skipped
        self.directories = {}
        self.files = {}

    @classmethod
    def for_plan(cls, plan):
        volume = plan.context["source_volume"].lower()
        if (os.name != "nt" or not volume.endswith(":exfat")
                or not volume.startswith("\\\\?\\volume{") or plan.source.startswith("\\\\")):
            return None
        skipped = {item.relative_path for item in plan.items if item.action == "skip"}
        if not skipped or 2 * len(skipped) < sum(
                state["kind"] == "file" for state in plan.context["source_entries"].values()):
            return None
        try:
            return cls(_WindowsReadCache(), skipped)
        except OSError:
            return None

    def watch_directory(self, relative, path):
        if not relative:
            self.cache.watch_tree(path)
        lock = self.cache.watch(path)
        self.directories[relative] = lock
        return lock is not None

    def watch_file(self, relative, path):
        if relative in self.skipped:
            self.files[relative] = self.cache.watch(path)

    def dirty_directories(self):
        if not self.cache.tree_intact():
            return {""}
        return {name for name, lock in self.directories.items() if lock is None or not lock.intact}

    def seal(self):
        self.cache.seal()

    def files_to_check(self, entries):
        for name, state in entries.items():
            if state["kind"] == "file":
                lock = self.files.get(name)
                if lock is None or not lock.intact:
                    yield name

    def close(self):
        self.cache.close()
