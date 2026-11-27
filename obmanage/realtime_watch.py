"""One outstanding recursive Windows directory notification, polled without blocking Qt."""
from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes as w

from .models import SyncError
from .paths import checked_child_snapshot, native
from .source_recheck import _Overlapped


def require_closed_writer(path):
    """Probe for an open writer; release immediately so the editor can keep saving."""
    if os.name != "nt":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
    create.restype = w.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = (w.HANDLE,), w.BOOL
    # FILE_READ_DATA participates in sharing checks; attribute-only opens do not.
    handle = create(native(path), 1, 1 | 4, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    close(handle)


def changed_names(data: bytes, directory_modified=None) -> set[str]:
    """An incomplete/unknown record requires a fresh inventory of the binding."""
    names = set()
    offset = 0
    records = 0
    while offset + 12 <= len(data):
        records += 1
        if records > 512:
            return {""}
        step, action, size = struct.unpack_from("<III", data, offset)
        end = offset + 12 + size
        if action not in (1, 2, 3, 4, 5) or not size or size % 2 or end > len(data):
            break
        name = data[offset + 12:end].decode("utf-16-le", "surrogatepass").replace("\\", "/")
        if any(p in ("", ".", "..") or ":" in p or "\x00" in p for p in name.split("/")):
            break
        if action != 3 or directory_modified is None or not directory_modified(name):
            names.add(name)
        if not step:
            return names
        if step % 4 or offset + step < end:
            break
        offset += step
    return {""}


class DirectoryWatch:
    def __init__(self, path):
        if os.name != "nt":
            raise OSError("当前系统不支持 Windows 目录监听")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        def bind(name, result, *args):
            proc = getattr(kernel, name)
            proc.argtypes, proc.restype = args, result
            return proc

        create = bind("CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                      w.DWORD, w.DWORD, w.HANDLE)
        event = bind("CreateEventW", w.HANDLE, w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR)
        self._read = bind("ReadDirectoryChangesW", w.BOOL, w.HANDLE, w.LPVOID, w.DWORD,
                          w.BOOL, w.DWORD, w.LPVOID, w.LPVOID, w.LPVOID)
        self._result = bind("GetOverlappedResult", w.BOOL, w.HANDLE, w.LPVOID,
                            ctypes.POINTER(w.DWORD), w.BOOL)
        self._cancel = bind("CancelIoEx", w.BOOL, w.HANDLE, w.LPVOID)
        self._close = bind("CloseHandle", w.BOOL, w.HANDLE)
        self._buffer = ctypes.create_string_buffer(64 * 1024)
        self.root = path
        self._io = _Overlapped()
        self._pending = False
        # FILE_LIST_DIRECTORY, shared read/write/delete, overlapped, open reparse point.
        self._handle = create(native(path), 1, 7, None, 3, 0x42200000, None)
        if self._handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._event = event(None, True, False, None)
            if not self._event:
                raise ctypes.WinError(ctypes.get_last_error())
            self._arm()
        except BaseException:
            self.close()
            raise

    def _arm(self):
        self._io = _Overlapped()
        self._io.event = self._event
        # Names, directory names, attributes, size, last write and creation time.
        ok = self._read(self._handle, self._buffer, len(self._buffer), True, 0x5F,
                        None, ctypes.byref(self._io), None)
        if not ok and ctypes.get_last_error() != 997:
            raise ctypes.WinError(ctypes.get_last_error())
        self._pending = True

    def poll(self) -> set[str]:
        names = set()
        # The first pending read often contains only a parent mtime change.
        # Drain already-completed buffers before the GUI's next tick.
        for _ in range(16):
            data = self._ready()
            if data is None:
                break
            names.update(changed_names(data, self._directory_metadata))
            if "" in names:
                return {""}
        return names

    def _directory_metadata(self, name):
        # Directory timestamps aren't mirrored. Structural actions (including both
        # halves of a rename) still enumerate the affected subtree.
        try:
            _, state = checked_child_snapshot(self.root, name)
            return state is not None and state["kind"] == "dir"
        except (OSError, SyncError):
            return False

    def _ready(self):
        size = w.DWORD()
        if not self._result(self._handle, ctypes.byref(self._io), ctypes.byref(size), False):
            error = ctypes.get_last_error()
            if error == 996:  # ERROR_IO_INCOMPLETE
                return None
            self._pending = False
            if error != 1022:  # ERROR_NOTIFY_ENUM_DIR
                raise ctypes.WinError(error)
        self._pending = False
        data = self._buffer.raw[:size.value]
        self._arm()
        return data

    def close(self):
        if self._handle is None:
            return
        if self._pending:
            self._cancel(self._handle, ctypes.byref(self._io))
            size = w.DWORD()
            self._result(self._handle, ctypes.byref(self._io), ctypes.byref(size), True)
            self._pending = False
        self._close(self._handle)
        self._handle = None
        if self._event:
            self._close(self._event)
