"""Read-only Windows runtime discovery. Saved ``open`` flags are not evidence."""
from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from ..models import SyncCancelled
from .catalog import discover_vaults, read_registered_vault_candidates
from .models import ManagementIssue, VaultCatalogResult


@dataclass(frozen=True)
class ObsidianWindow:
    handle: int
    process_id: int
    title: str
    executable: str


def obsidian_windows() -> tuple[ObsidianWindow, ...]:
    """Enumerate visible (including minimized) windows owned by Obsidian.exe.

    No process launch, focus change, debug port, plugin, or vault write is needed.
    QueryFullProcessImageName checks the owner, not just a matching title.
    """
    if os.name != "nt":
        raise OSError("当前运行仓库识别仅支持 Windows。")
    from ctypes import wintypes as w
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.EnumWindows.restype = w.BOOL
    user.IsWindowVisible.argtypes = [w.HWND]
    user.GetWindowTextLengthW.argtypes = [w.HWND]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    found = []
    executables = {}
    failures = []

    @callback_type
    def visit(hwnd, _):
        try:
            if not user.IsWindowVisible(hwnd):
                return True
            length = user.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user.GetWindowTextW(hwnd, title, len(title))
            if "Obsidian" not in title.value:
                return True
            pid = w.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in executables:
                handle = kernel.OpenProcess(0x1000, False, pid.value)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = w.DWORD(len(buffer))
                    if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    executables[pid.value] = buffer.value
                finally:
                    kernel.CloseHandle(handle)
            executable = executables[pid.value]
            if os.path.basename(executable).casefold() == "obsidian.exe":
                found.append(ObsidianWindow(int(hwnd), pid.value, title.value, executable))
        except OSError as exc:
            failures.append(str(exc))
        return True

    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if failures:
        raise OSError("无法核验部分 Obsidian 窗口的所属进程：" + failures[0])
    return tuple(found)


def match_running_vaults(windows, registered, *, cancel: Event | None = None) -> VaultCatalogResult:
    """Match complete title suffixes; ambiguous names never select a random path."""
    issues = list(registered.issues)
    matched = set()
    for window in windows:
        if cancel is not None and cancel.is_set():
            raise SyncCancelled("已取消识别当前运行仓库。")
        # Keep the full prefix: splitting on ' - ' would break vault/note names.
        title = re.sub(r" - Obsidian(?: v?\d[^\r\n]*)?$", "", window.title)
        if title == window.title:
            issues.append(ManagementIssue("runtime_title_unknown", "有运行窗口无法识别仓库名称，请从列表选择或浏览。"))
            continue
        matches = [path for path in registered.paths
                   if title == Path(path).name or title.endswith(" - " + Path(path).name)]
        # 'A - B' and 'B' can both be suffixes: there is no safe way to guess.
        if len(matches) == 1:
            matched.add(matches[0])
        elif matches:
            issues.append(ManagementIssue("runtime_ambiguous", "运行窗口对应多个同名或名称有歧义的仓库，请从列表核对完整路径。"))
        else:
            issues.append(ManagementIssue("runtime_unregistered", "有运行窗口未匹配到已登记仓库，请浏览选择其完整路径。"))
    validated = discover_vaults(sorted(matched), recursive=False, cancel=cancel)
    issues.extend(validated.issues)
    if matched and len(validated.vaults) < len(matched):
        issues.append(ManagementIssue("runtime_invalid_vault", "部分运行窗口的登记路径已失效或不再是仓库。"))
    return VaultCatalogResult(validated.vaults, tuple(issues))


def read_running_vaults(config_path=None, *, cancel=None) -> VaultCatalogResult:
    try:
        windows = obsidian_windows()
    except OSError as exc:
        return VaultCatalogResult(issues=(ManagementIssue("runtime_unavailable", str(exc)),))
    if not windows:
        return VaultCatalogResult(issues=(ManagementIssue(
            "runtime_not_found", "未发现正在运行的 Obsidian 仓库窗口，请先在 Obsidian 中打开仓库。", severity="warning"),))
    registered = read_registered_vault_candidates(config_path)
    result = match_running_vaults(windows, registered, cancel=cancel)
    # Window closure or title switch during directory validation invalidates the result.
    try:
        current = obsidian_windows()
    except OSError as exc:
        return VaultCatalogResult(issues=(ManagementIssue("runtime_unavailable", str(exc)),))
    if set(windows) != set(current):
        return VaultCatalogResult(issues=(ManagementIssue(
            "runtime_changed", "Obsidian 窗口在识别期间改变，请重新识别。"),))
    return result
