"""Keep diagnostic writes physically separate from both live input trees."""
from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from obmanage.models import SyncError
from obmanage.paths import assert_plain_chain, canonical, identity, is_reparse, native, snapshot, volume_identity
from tools.benchmark_sandbox import test_parent


class BoundaryError(ValueError):
    pass


def _absolute(path):
    path = Path(path)
    if (not path.is_absolute() or path == path.parent or ".." in path.parts
            or (os.name == "nt" and (len(path.drive) != 2 or path.drive[1] != ":"
                or any(":" in part or part.rstrip(". ") != part for part in path.parts[1:])))):
        raise BoundaryError("Expected an absolute local path below the volume root")
    return path


def _binding(path):
    state = assert_plain_chain(path)
    if state is None or not state["inode"]:
        raise BoundaryError("Directory unavailable or identity unverifiable")
    return volume_identity(str(path)), identity(state)


@dataclass(frozen=True)
class Directory:
    requested: Path
    physical: Path
    binding: tuple

    @classmethod
    def bind(cls, path):
        path = _absolute(path)
        expected = _binding(path)
        physical = _absolute(canonical(os.path.realpath(native(path), strict=True)))
        if _binding(physical) != expected or _binding(path) != expected:
            raise BoundaryError("Directory changed during physical resolution")
        return cls(path, physical, expected)

    def verify(self):
        if (_binding(self.requested) != self.binding
                or Path(canonical(os.path.realpath(native(self.requested), strict=True))) != self.physical
                or _binding(self.physical) != self.binding):
            raise BoundaryError("Directory or volume identity changed")

    @contextmanager
    def hold(self):
        """Keep writable ancestors from being renamed while using absolute paths."""
        self.verify()
        handles = []
        try:
            if os.name == "nt":
                from ctypes import wintypes

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                create = kernel.CreateFileW
                create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
                create.restype = wintypes.HANDLE
                close = kernel.CloseHandle
                close.argtypes = [wintypes.HANDLE]
                for path in (*reversed(self.physical.parents), self.physical):
                    if path == path.parent:
                        continue
                    # LIST_DIRECTORY | READ_ATTRIBUTES, share read/write (no delete), OPEN_EXISTING,
                    # BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
                    handle = create(native(path), 0x81, 3, None, 3, 0x02200000, None)
                    if handle == ctypes.c_void_p(-1).value:
                        raise ctypes.WinError(ctypes.get_last_error())
                    handles.append(handle)
            self.verify()
            yield
        finally:
            for handle in reversed(handles):
                close(handle)


def _overlaps(first, second):
    a, b = first.physical, second.physical
    if first.binding == second.binding or a == b or a in b.parents or b in a.parents:
        return True
    if first.binding[0] != second.binding[0]:
        return False
    # Compare actual directory IDs on a common volume, including drive aliases
    # whose spellings do not share an os.path.commonpath().
    return any(identity(snapshot(parent)) == boundary.binding[1]
               for child, boundary in ((a, second), (b, first)) for parent in child.parents)


def system_temp():
    """Resolve the Windows temp location without tempfile's test-file probe."""
    if os.name != "nt":
        raise BoundaryError("Real-device diagnostics require Windows volume identity APIs")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_unicode_buffer(32768)
    get_temp = getattr(kernel, "GetTempPath2W", kernel.GetTempPathW)
    if not 0 < get_temp(len(buffer), buffer) < len(buffer):
        raise BoundaryError("Windows temporary directory is unavailable")
    temp = Directory.bind(buffer.value)
    if not 0 < kernel.GetSystemWindowsDirectoryW(buffer, len(buffer)) < len(buffer):
        raise BoundaryError("Windows system directory is unavailable")
    system = Directory.bind(buffer.value)
    if temp.binding[0] != system.binding[0]:
        raise BoundaryError("Use a temporary directory on the Windows system volume")
    return temp.physical


class Protection:
    def __init__(self, local, portable, temp):
        self.inputs = {}
        for role, path in (("local", local), ("portable", portable)):
            try:
                self.inputs[role] = Directory.bind(path)
            except (OSError, SyncError, BoundaryError) as exc:
                raise BoundaryError(f"{role} scan scope unavailable or identity unverifiable") from exc
        if _overlaps(*self.inputs.values()):
            raise BoundaryError("The two scan scopes are identical or overlap")
        self.temp = self.writable(temp)

    def verify(self):
        for role, root in self.inputs.items():
            try:
                root.verify()
            except (OSError, SyncError, BoundaryError) as exc:
                raise BoundaryError(f"{role} directory or volume identity changed") from exc

    def outside(self, path):
        self.verify()
        directory = Directory.bind(path)
        if any(_overlaps(directory, root) for root in self.inputs.values()):
            raise BoundaryError("Output or scratch directory intersects a protected scan scope")
        return directory

    def writable(self, path):
        directory = self.outside(path)
        test_parent(directory.physical)
        return directory

    def report_path(self, path):
        path = _absolute(path)
        self.temp.verify()
        parent = self.writable(path.parent)
        if parent.requested != parent.physical:
            raise BoundaryError("Use the physical spelling of the output parent")
        if (parent.binding[0] != self.temp.binding[0]
                or not (parent.physical == self.temp.physical or self.temp.physical in parent.physical.parents)):
            raise BoundaryError("Place the report in the verified system temporary directory")
        target = parent.physical / path.name
        if snapshot(target) is not None:
            raise BoundaryError("The report path already exists")
        return parent, target

    def inventory_epoch(self, cancel, roles=None):
        """Independently detect protected aliases and input drift, without reading content.

        Scan every directory, including those excluded by business policies. A
        reparse target outside the two trees can expose unbounded additional
        protected paths. In that case no report or scratch tree may be created.
        """
        self.verify()
        scopes = tuple(root.physical for root in self.inputs.values())
        epochs = {}
        for role in self.inputs if roles is None else roles:
            root = self.inputs[role]
            digest = hashlib.sha256()
            pending = [root.physical]
            while pending:
                if cancel.is_set():
                    raise InterruptedError
                folder = pending.pop()
                before = assert_plain_chain(folder)
                if before is None or before["kind"] != "dir":
                    raise BoundaryError("A protected directory changed during preflight")
                children = []
                children_data = []
                with os.scandir(native(folder)) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            raise InterruptedError
                        state = entry.stat(follow_symlinks=False)
                        child = folder / entry.name
                        alias = None
                        if is_reparse(state):
                            target = Path(canonical(os.path.realpath(native(child), strict=True)))
                            assert_plain_chain(target.parent)
                            if (target == child or
                                    not any(target == scope or scope in target.parents for scope in scopes)):
                                raise BoundaryError("A protected reparse target extends outside the verified scopes")
                            alias = str(target)
                        elif stat.S_ISDIR(state.st_mode):
                            children.append(child)
                            # Windows enumeration can return lagged directory
                            # timestamps. The walk binds each child separately.
                            continue
                        entry_data = (entry.name, state.st_mode, state.st_size,
                                      state.st_mtime_ns, state.st_ctime_ns, state.st_ino, alias)
                        children_data.append(repr(entry_data).encode("utf-8", "surrogatepass"))
                if assert_plain_chain(folder) != before:
                    raise BoundaryError("A protected directory changed during preflight")
                digest.update(os.fsencode(str(folder)))
                digest.update(repr(before).encode("ascii"))
                for value in sorted(children_data):
                    digest.update(len(value).to_bytes(8, "little"))
                    digest.update(value)
                pending.extend(sorted(children, reverse=True))
            epochs[role] = digest.digest()
        self.verify()
        return epochs
