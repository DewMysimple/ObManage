"""Path, volume and identity checks shared by the mirror engine.

All filesystem operations use extended Windows paths. Reparse points are rejected
instead of traversed; the mirror consequently never treats a junction as a folder.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .models import SyncError
from .scanning.metrics import current, measured


try:
    _get_file_attributes = ctypes.windll.kernel32.GetFileAttributesW if os.name == "nt" else None
except (AttributeError, OSError):
    _get_file_attributes = None
if _get_file_attributes is not None:
    _get_file_attributes.argtypes = [ctypes.c_wchar_p]
    _get_file_attributes.restype = ctypes.c_uint32


def native(path: str | Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def canonical(path: str | Path) -> str:
    value = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normpath(value)


def is_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


@measured("snapshot", calls=True)
def snapshot(path: str | Path) -> dict | None:
    path = native(path)
    metrics = current()
    try:
        st = os.lstat(path) if metrics is None else metrics.query("lstat", os.lstat, path)
    except FileNotFoundError:
        return None
    if is_reparse(st):
        kind = "link"
    elif stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "special"
    return {"kind": kind, "size": st.st_size if kind == "file" else 0,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
            "inode": st.st_ino, "device": st.st_dev}


def identity(state: dict | None) -> tuple | None:
    if state is None:
        return None
    return state["kind"], state["device"], state["inode"]


def _create_windows_directory(path, share):
    """Create a new directory and obtain its non-deletable handle in one call."""
    from ctypes import wintypes as w

    class UnicodeString(ctypes.Structure):
        _fields_ = (("length", w.USHORT), ("capacity", w.USHORT), ("text", w.LPWSTR))

    class ObjectAttributes(ctypes.Structure):
        _fields_ = (("length", w.ULONG), ("root", w.HANDLE),
                    ("name", ctypes.POINTER(UnicodeString)), ("flags", w.ULONG),
                    ("security", w.LPVOID), ("quality", w.LPVOID))

    dll = ctypes.WinDLL("ntdll")
    create = dll.NtCreateFile
    create.argtypes = (ctypes.POINTER(w.HANDLE), w.ULONG, ctypes.POINTER(ObjectAttributes),
                       w.LPVOID, w.LPVOID, w.ULONG, w.ULONG, w.ULONG, w.ULONG, w.LPVOID, w.ULONG)
    create.restype = w.LONG
    convert = dll.RtlNtStatusToDosError
    convert.argtypes = (w.LONG,)
    convert.restype = w.ULONG
    name = "\\??\\" + native(path)[4:]
    length = len(name.encode("utf-16-le"))
    name_buffer = ctypes.create_unicode_buffer(name)
    text = UnicodeString(length, length + 2, ctypes.cast(name_buffer, w.LPWSTR))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), None, ctypes.pointer(text),
                                  0x1040, None, None)  # DONT_REPARSE | CASE_INSENSITIVE
    status_block = (ctypes.c_size_t * 2)()
    handle = w.HANDLE()
    # FILE_CREATE (exclusive) and FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT.
    status = create(ctypes.byref(handle), 0x100081, ctypes.byref(attributes), status_block,
                    None, 0, share, 2, 0x21, None, 0)
    if status < 0:
        raise ctypes.WinError(convert(status))
    return handle


@contextmanager
def hold_windows_directory(path: str | Path, expected: tuple | None = None, *, create=False):
    """Pin one ordinary directory while creating children through its path."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create_file.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    # LIST_DIRECTORY | READ_ATTRIBUTES, share read/write (no delete), OPEN_EXISTING,
    # BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    if create:
        handle = _create_windows_directory(path, 3)
    else:
        handle = create_file(native(path), 0x81, 3, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        actual = identity(snapshot(path))
        if (not actual or actual[0] != "dir" or not actual[2]
                or (not create and actual != expected)):
            raise SyncError(f"无法固定新增范围内的目录身份：{path}")
        yield actual
    finally:
        close(handle)


@contextmanager
def open_windows_stage(path: str, expected: tuple):
    """Keep readback, attribute changes and publication on the owned temp handle."""
    import msvcrt
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
    create.restype = w.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (w.HANDLE,)
    # GENERIC_READ | DELETE | WRITE_ATTRIBUTES, share read only,
    # OPEN_EXISTING | OPEN_REPARSE_POINT.
    handle = create(native(path), 0x80010100, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            state = os.fstat(fd)
            if (("file", state.st_dev, state.st_ino) != expected or is_reparse(state)
                    or not stat.S_ISREG(state.st_mode)):
                raise SyncError(f"暂存文件所有权已改变：{path}")
            yield stream, handle
    finally:
        os.close(fd)


def set_windows_stage_times(handle, access_ns, modification_ns):
    from ctypes import wintypes as w

    def file_time(nanoseconds):
        ticks = nanoseconds // 100 + 116444736000000000
        return w.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)

    set_time = ctypes.WinDLL("kernel32", use_last_error=True).SetFileTime
    set_time.argtypes = (w.HANDLE, ctypes.POINTER(w.FILETIME),
                         ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME))
    set_time.restype = w.BOOL
    access, modification = file_time(access_ns), file_time(modification_ns)
    if not set_time(handle, None, ctypes.byref(access), ctypes.byref(modification)):
        raise ctypes.WinError(ctypes.get_last_error())


def _set_windows_file_information(handle, kind, buffer, size):
    from ctypes import wintypes as w

    setter = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    setter.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD)
    setter.restype = w.BOOL
    if not setter(handle, kind, buffer, size):
        raise ctypes.WinError(ctypes.get_last_error())


def publish_windows_stage(handle, target):
    """Rename the already-open file with ReplaceIfExists disabled."""
    from ctypes import wintypes as w

    class RenameInfo(ctypes.Structure):
        _fields_ = (("flags", w.DWORD), ("root", w.HANDLE), ("length", w.DWORD), ("name", w.WCHAR))

    name = native(target).encode("utf-16-le")
    buffer = ctypes.create_string_buffer(max(ctypes.sizeof(RenameInfo), RenameInfo.name.offset + len(name) + 2))
    info = RenameInfo.from_buffer(buffer)
    info.length = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.name.offset, name, len(name))
    _set_windows_file_information(handle, 3, buffer, ctypes.sizeof(buffer))  # FileRenameInfo


def discard_windows_stage(path, expected):
    with open_windows_stage(path, expected) as (_, handle):
        delete = ctypes.c_int(1)
        _set_windows_file_information(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete))  # FileDispositionInfo


@measured("plain_chain", calls=True)
def assert_plain_chain(path: str | Path, *, leaf_snapshotter=None,
                       attributes_only_ancestors=False) -> dict | None:
    """Check the live chain through the leaf and return that leaf's snapshot."""
    leaf = value = canonical(path)
    parents = []
    while True:
        parent = os.path.dirname(value)
        if parent == value:
            break
        parents.append(parent)
        value = parent
    attributes = _get_file_attributes if attributes_only_ancestors else None
    metrics = current() if attributes is not None else None
    for part in reversed(parents):
        if attributes is not None and len(part) > 3 and part[1:3] == ":\\":
            # Ordinary local ancestors need only a live kind check. The API
            # reports attributes of the link itself, including mount points.
            # Everything except a plain directory takes the original path,
            # preserving missing-path and filesystem error handling.
            extended = "\\\\?\\" + part
            flags = (attributes(extended) if metrics is None else
                     metrics.query("file_attributes", attributes, extended))
            if flags & (stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT
                        | stat.FILE_ATTRIBUTE_DEVICE) == stat.FILE_ATTRIBUTE_DIRECTORY:
                continue
        state = snapshot(part)
        if state is not None and state["kind"] != "dir":
            raise SyncError(f"路径包含链接或非目录，无法安全同步：{part}")
    state = (leaf_snapshotter or snapshot)(leaf)
    if state is not None and state["kind"] != "dir":
        raise SyncError(f"路径包含链接或非目录，无法安全同步：{leaf}")
    return state


def validate_state_separation(state_dir: str | Path, roots: tuple[str, ...] | list[str]) -> str:
    """Validate, without writing, that durable app state is outside repository roots."""
    requested_state = canonical(state_dir)
    assert_plain_chain(requested_state)
    resolved_state = canonical(os.path.realpath(native(requested_state)))
    assert_plain_chain(resolved_state)
    state_key = os.path.normcase(resolved_state)
    for raw_root in roots:
        if not str(raw_root).strip():
            continue
        requested_root = canonical(raw_root)
        assert_plain_chain(requested_root)
        resolved_root = canonical(os.path.realpath(native(requested_root)))
        assert_plain_chain(resolved_root)
        root_key = os.path.normcase(resolved_root)
        try:
            common = os.path.commonpath((state_key, root_key))
        except ValueError:
            continue
        if common in (state_key, root_key):
            raise SyncError(
                "程序数据目录必须与仓库路径分离，不能相同或互相包含。"
            )
    return resolved_state


def checked_child_snapshot(root: str, relative: str) -> tuple[str, dict | None]:
    """Return a safe child path and the snapshot observed by that same check.

    Callers that must immediately compare the child with a preview can reuse
    the snapshot instead of issuing a second, adjacent ``lstat``.  The full
    ancestor-chain and reparse-point checks remain identical to
    :func:`checked_child`.
    """
    candidate = lexical_child_path(root, relative)
    assert_plain_chain(os.path.dirname(candidate))
    state = snapshot(candidate)
    if state is not None and state["kind"] in ("link", "special"):
        raise SyncError(f"路径已变成链接或特殊文件：{relative}")
    return candidate, state


def lexical_child_path(root: str, relative: str) -> str:
    """Lexical containment only; callers must independently check the live chain."""
    parts = relative.replace("\\", "/").split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        raise SyncError(f"无效的相对路径：{relative}")
    if os.name == "nt" and any(":" in part for part in parts):
        raise SyncError(f"无效的相对路径：{relative}")
    candidate = canonical(os.path.join(root, *parts))
    if os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) != os.path.normcase(root):
        raise SyncError(f"文件路径越出镜像范围：{relative}")
    return candidate


def checked_child(root: str, relative: str) -> str:
    return checked_child_snapshot(root, relative)[0]


def existing_anchor(path: str) -> str:
    current = path
    while snapshot(current) is None:
        parent = os.path.dirname(current)
        if parent == current:
            raise SyncError(f"磁盘未连接或路径不可用：{path}")
        current = parent
    return current


def volume_identity(path: str) -> str:
    anchor = existing_anchor(path)
    if os.name != "nt":
        anchor = native(anchor)
        metrics = current()
        state = os.stat(anchor) if metrics is None else metrics.query("stat", os.stat, anchor)
        return f"device:{state.st_dev}"
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_path = kernel.GetVolumePathNameW
    get_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_path.restype = wintypes.BOOL
    get_info = kernel.GetVolumeInformationW
    get_info.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                         ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(32768)
    if not get_path(native(anchor), buffer, len(buffer)):
        raise SyncError(f"无法识别磁盘：{path}（{ctypes.WinError(ctypes.get_last_error())}）")
    serial, max_component, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    filesystem = ctypes.create_unicode_buffer(128)
    if not get_info(buffer.value, None, 0, ctypes.byref(serial), ctypes.byref(max_component),
                    ctypes.byref(flags), filesystem, len(filesystem)):
        raise SyncError(f"无法读取磁盘身份：{path}（{ctypes.WinError(ctypes.get_last_error())}）")
    # Include the volume GUID where available (local drives); UNC shares retain
    # their share root as identity in addition to the serial and filesystem.
    guid = ctypes.create_unicode_buffer(1024)
    get_guid = kernel.GetVolumeNameForVolumeMountPointW
    get_guid.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_guid.restype = wintypes.BOOL
    name = guid.value if get_guid(buffer.value, guid, len(guid)) else buffer.value
    if guid.value:
        name = guid.value
    return f"{name.casefold()}:{serial.value:08x}:{filesystem.value}"


def pair_identity(source: str, target: str, source_volume: str, target_volume: str) -> str:
    """An ordered path/volume key, kept compatible with existing local records."""
    return hashlib.sha256(json.dumps(
        [os.path.normcase(source), os.path.normcase(target), source_volume, target_volume],
        ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_roots(source: str, target: str) -> dict:
    if not source.strip() or not target.strip():
        raise SyncError("请选择源目录和目标目录。")
    source, target = canonical(source), canonical(target)
    # Validate the user-selected chain before realpath can follow a link; then
    # collapse Windows 8.3 aliases so differently spelled physical overlaps
    # cannot bypass the containment check.
    assert_plain_chain(source)
    assert_plain_chain(target)
    source = canonical(os.path.realpath(native(source)))
    target = canonical(os.path.realpath(native(target)))
    src_key, dst_key = os.path.normcase(source), os.path.normcase(target)
    if Path(target).parent == Path(target):
        raise SyncError("目标不能是磁盘根目录，请选择专用镜像文件夹。")
    try:
        common = os.path.commonpath((src_key, dst_key))
    except ValueError:
        common = ""
    if common in (src_key, dst_key):
        raise SyncError("源目录与目标目录不能相同，也不能互相包含。")
    assert_plain_chain(source)
    assert_plain_chain(target)
    src_state, dst_state = snapshot(source), snapshot(target)
    if src_state is None or src_state["kind"] != "dir":
        raise SyncError(f"源目录不存在或磁盘未连接：{source}")
    anchor = existing_anchor(target)
    src_volume, dst_volume = volume_identity(source), volume_identity(target)
    pair_id = pair_identity(source, target, src_volume, dst_volume)
    return {"source": source, "target": target, "pair_id": pair_id,
            "reverse_pair_id": pair_identity(target, source, dst_volume, src_volume),
            "source_volume": src_volume, "target_volume": dst_volume,
            "source_root": src_state, "target_root": dst_state,
            "target_anchor": anchor, "target_anchor_state": snapshot(anchor)}


def revalidate_roots(context: dict, *, target_root: dict | None = None) -> None:
    source, target = context["source"], context["target"]
    assert_plain_chain(source)
    assert_plain_chain(target)
    if volume_identity(source) != context["source_volume"] or volume_identity(target) != context["target_volume"]:
        raise SyncError("磁盘身份已改变，请重新分析差异。")
    if identity(snapshot(source)) != identity(context["source_root"]):
        raise SyncError("源目录已被替换或移除，请重新分析差异。")
    expected = target_root if target_root is not None else context["target_root"]
    if identity(snapshot(target)) != identity(expected):
        raise SyncError("目标目录已被替换或移除，请重新分析差异。")
    anchor = context["target_anchor"]
    if identity(snapshot(anchor)) != identity(context["target_anchor_state"]):
        raise SyncError("目标目录所在位置已改变，请重新分析差异。")
