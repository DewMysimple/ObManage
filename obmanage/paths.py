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
from pathlib import Path

from .models import SyncError


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


def snapshot(path: str | Path) -> dict | None:
    try:
        st = os.lstat(native(path))
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


def assert_plain_chain(path: str | Path) -> None:
    """Reject existing non-directory/reparse ancestors, including the root itself."""
    value = Path(canonical(path))
    for part in reversed((value, *value.parents)):
        state = snapshot(part)
        if state is None:
            continue
        if state["kind"] != "dir":
            raise SyncError(f"路径包含链接或非目录，无法安全同步：{part}")


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
    parts = relative.replace("\\", "/").split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        raise SyncError(f"无效的相对路径：{relative}")
    if os.name == "nt" and any(":" in part for part in parts):
        raise SyncError(f"无效的相对路径：{relative}")
    candidate = canonical(os.path.join(root, *parts))
    if os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) != os.path.normcase(root):
        raise SyncError(f"文件路径越出镜像范围：{relative}")
    assert_plain_chain(os.path.dirname(candidate))
    state = snapshot(candidate)
    if state is not None and state["kind"] in ("link", "special"):
        raise SyncError(f"路径已变成链接或特殊文件：{relative}")
    return candidate, state


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
        return f"device:{os.stat(native(anchor)).st_dev}"
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
