"""Windows Obsidian shutdown and exact-path reopening, without Qt.

Saved ``open`` flags describe the previous session as well as live sessions.
Shutdown therefore checks every process, including windowless processes, and
never edits Obsidian's registry. Termination uses an already verified process
handle rather than a PID or a shell's image-name matching command.
"""
from __future__ import annotations

import ctypes
import json
import ntpath
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from ..models import Progress, SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, snapshot
from .catalog import _read_stable_file, discover_vaults


class ObsidianLifecycleError(SyncError):
    """Obsidian's runtime state could not be changed or verified safely."""


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("已取消关闭 Obsidian。")


class _WindowsProcess:
    def __init__(self, kernel, handle, pid, executable):
        self.kernel = kernel
        self.handle = handle
        self.pid = pid
        self.executable = executable

    def running(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0:
            return False
        if result == 258:
            return True
        raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self):
        if self.running() and not self.kernel.TerminateProcess(self.handle, 1):
            if self.running():
                raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        self.kernel.CloseHandle(self.handle)


def _windows_processes():
    """Return owned handles to all precisely named, image-verified instances."""
    if os.name != "nt":
        raise ObsidianLifecycleError("自动关闭 Obsidian 仅支持 Windows。")
    from ctypes import wintypes as w

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                    ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG),
                    ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = w.BOOL
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = w.BOOL
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    kernel.TerminateProcess.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    snapshot_handle = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if snapshot_handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot_handle, ctypes.byref(entry))
        while available:
            if entry.szExeFile.casefold() == "obsidian.exe":
                # QUERY_LIMITED_INFORMATION | SYNCHRONIZE | TERMINATE. An
                # inaccessible candidate is an error, never proof of absence.
                handle = kernel.OpenProcess(0x1000 | 0x100000 | 0x1, False, entry.th32ProcessID)
                if not handle:
                    error = ctypes.get_last_error()
                    if error != 87:  # The snapshot's process has already exited.
                        raise ctypes.WinError(error)
                else:
                    try:
                        executable = ctypes.create_unicode_buffer(32768)
                        length = w.DWORD(len(executable))
                        if not kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(length)):
                            raise ctypes.WinError(ctypes.get_last_error())
                        if ntpath.basename(executable.value).casefold() != "obsidian.exe":
                            raise ObsidianLifecycleError("Obsidian 进程身份发生变化，请重新执行。")
                        result.append(_WindowsProcess(kernel, handle, int(entry.th32ProcessID), executable.value))
                    except BaseException:
                        kernel.CloseHandle(handle)
                        raise
            available = kernel.Process32NextW(snapshot_handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        for process in result:
            process.close()
        raise
    finally:
        kernel.CloseHandle(snapshot_handle)
    return tuple(result)


def _request_windows_close(processes):
    """Ask all windows (including hidden windows) of verified instances to close."""
    from ctypes import wintypes as w
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows.restype = w.BOOL
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowThreadProcessId.restype = w.DWORD
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.PostMessageW.restype = w.BOOL
    by_pid = {process.pid: process for process in processes}
    failures = []

    @callback_type
    def visit(hwnd, _):
        try:
            pid = w.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = by_pid.get(pid.value)
            if process is not None and process.running():
                user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except OSError as exc:
            failures.append(exc)
        return True

    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if failures:
        raise failures[0]


def _wait_for_exit(processes, timeout, cancel):
    deadline = time.monotonic() + timeout
    while True:
        _check_cancel(cancel)
        if not any(process.running() for process in processes):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if cancel is None:
            time.sleep(min(0.1, remaining))
        else:
            cancel.wait(min(0.1, remaining))


def assert_obsidian_closed():
    """Fail closed on a live process or an unreliable process inventory."""
    processes = ()
    try:
        processes = _windows_processes()
        if any(process.running() for process in processes):
            raise ObsidianLifecycleError("Obsidian 仍在运行或已重新启动，已停止同步，请重新执行。")
    except OSError as exc:
        raise ObsidianLifecycleError(f"无法核验 Obsidian 已退出：{exc}") from exc
    finally:
        for process in processes:
            process.close()


def close_obsidian(*, cancel=None, progress=None, graceful_timeout=5.0, force_timeout=5.0):
    """Close all Obsidian processes, force remaining verified handles if needed.

    Call only after the user starts a synchronization whose displayed behavior
    includes application shutdown. No vault contents or saved flags are edited.
    Cancellation may leave Obsidian closed, but always stops subsequent work.
    """
    if graceful_timeout < 0 or force_timeout < 0:
        raise ValueError("等待时间不能为负数。")
    _check_cancel(cancel)
    processes = ()
    try:
        processes = _windows_processes()
        if processes:
            if progress:
                progress(Progress("closing_obsidian", "正在关闭 Obsidian…"))
            _request_windows_close(processes)
            if not _wait_for_exit(processes, graceful_timeout, cancel):
                if progress:
                    progress(Progress("closing_obsidian", "正在强制结束未退出的 Obsidian…"))
                for process in processes:
                    _check_cancel(cancel)
                    process.terminate()
                if not _wait_for_exit(processes, force_timeout, cancel):
                    raise ObsidianLifecycleError("Obsidian 未在限定时间内退出，已停止同步。")
        _check_cancel(cancel)
        assert_obsidian_closed()
        return len(processes)
    except OSError as exc:
        raise ObsidianLifecycleError(f"无法安全关闭 Obsidian，已停止同步：{exc}") from exc
    finally:
        for process in processes:
            process.close()


def _open_uri(uri):
    if os.name != "nt":
        raise OSError("自动打开 Obsidian 仅支持 Windows。")
    os.startfile(uri)


def _registered_vault_id(root, config_path):
    """Read a stable registry and require one exact path-to-ID association."""
    if config_path is None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ObsidianLifecycleError("无法定位 Obsidian 登记文件：APPDATA 未设置。")
        config_path = Path(appdata) / "obsidian" / "obsidian.json"
    config = canonical(config_path)
    assert_plain_chain(os.path.dirname(config))
    state = snapshot(config)
    if state is None:
        raise ObsidianLifecycleError("未找到 Obsidian 登记文件，请先在 Obsidian 中打开该仓库一次。")
    if state["kind"] != "file":
        raise ObsidianLifecycleError("Obsidian 登记文件必须是普通文件，不能是链接或特殊项。")
    try:
        payload = json.loads(_read_stable_file(config, state).decode("utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ObsidianLifecycleError(f"无法安全读取 Obsidian 登记文件：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("vaults"), dict):
        raise ObsidianLifecycleError("Obsidian 登记文件缺少有效的 vaults 对象。")
    matches = []
    for vault_id, entry in payload["vaults"].items():
        value = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise ObsidianLifecycleError("Obsidian 登记文件存在缺少有效路径的仓库记录。")
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not os.path.isabs(expanded):
            raise ObsidianLifecycleError("Obsidian 登记文件存在使用相对路径的仓库记录。")
        if os.path.normcase(canonical(expanded)) == os.path.normcase(root):
            # URI's vault parameter also accepts names. Reject malformed IDs
            # rather than accidentally opening another vault with that name.
            if re.fullmatch(r"[0-9a-fA-F]{16}", vault_id) is None:
                raise ObsidianLifecycleError("该仓库的 Obsidian 登记 ID 无效，请在 Obsidian 中重新登记仓库。")
            matches.append(vault_id)
    if not matches:
        raise ObsidianLifecycleError("仓库尚未登记到 Obsidian，请先在 Obsidian 中打开该仓库一次。")
    if len(matches) != 1:
        raise ObsidianLifecycleError("同一仓库路径对应多个 Obsidian 登记 ID，无法确定应打开哪一项。")
    return matches[0]


def open_vaults(paths, *, config_path=None):
    """Open exact registered vault IDs; return launch failures separately.

    Matching full paths in the safely read registry avoids ambiguous vault
    names. Successful ShellExecute means a request was accepted, not that a
    window is already ready. Neither the registry nor vault contents are edited.
    """
    failures = []
    seen = set()
    for path in paths:
        try:
            root = canonical(path)
            key = os.path.normcase(root)
            if key in seen:
                continue
            seen.add(key)
            catalog = discover_vaults((root,), recursive=False)
            if catalog.issues or len(catalog.vaults) != 1:
                detail = catalog.issues[0].message if catalog.issues else "目录已不再是有效仓库。"
                raise ObsidianLifecycleError(detail)
            vault_id = _registered_vault_id(root, config_path)
            _open_uri("obsidian://open?vault=" + quote(vault_id, safe=""))
        except Exception as exc:
            # Opening follows a completed transaction. Report separately and
            # continue with the other selected vaults; do not lose its success.
            failures.append(f"无法自动打开 {path}：{exc}")
    return tuple(failures)
