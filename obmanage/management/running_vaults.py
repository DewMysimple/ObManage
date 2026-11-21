"""Windows vault sessions and their window identities. Saved ``open`` flags are not evidence."""
from __future__ import annotations

import ctypes
import ntpath
import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from ..models import SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, identity, snapshot
from .catalog import discover_vaults, read_registered_vault_candidates
from .models import ManagementIssue, VaultCatalogResult, VaultInfo


@dataclass(frozen=True)
class ObsidianWindow:
    handle: int
    process_id: int
    title: str
    executable: str
    process_started: int
    thread_id: int
    window_class: str
    owner: int = 0
    visible: bool = True

    @property
    def key(self):
        return (self.handle, self.process_id, self.process_started, self.thread_id,
                os.path.normcase(self.executable), self.window_class)


@dataclass(frozen=True)
class VaultSession:
    vault: VaultInfo
    directory_identity: tuple
    windows: tuple[ObsidianWindow, ...]


@dataclass(frozen=True)
class UnresolvedWindow:
    issue: ManagementIssue
    paths: tuple[str, ...] = ()
    window: ObsidianWindow | None = None


@dataclass(frozen=True)
class RuntimeSnapshot:
    sessions: tuple[VaultSession, ...] = ()
    unresolved: tuple[UnresolvedWindow, ...] = ()
    notices: tuple[ManagementIssue, ...] = ()
    windows: tuple[ObsidianWindow, ...] | None = None

    @property
    def issues(self):
        return tuple(entry.issue for entry in self.unresolved) + self.notices

    def catalog(self) -> VaultCatalogResult:
        return VaultCatalogResult(tuple(session.vault for session in self.sessions), self.issues)

    def session(self, path: str) -> VaultSession | None:
        key = path_key(path)
        for session in self.sessions:
            if path_key(session.vault.path) == key:
                return session
        if self.sessions:
            target_identity = _existing_vault_identity(path)
            return next((session for session in self.sessions if session.directory_identity == target_identity), None)
        return None

    def blockers(self, paths) -> tuple[ManagementIssue, ...]:
        paths = tuple(paths)
        if not paths or not self.unresolved:
            return ()
        keys = {path_key(path) for path in paths}
        identities = {_existing_vault_identity(path) for path in paths} - {None}
        return tuple(entry.issue for entry in self.unresolved if (
            not entry.paths or keys.intersection(map(path_key, entry.paths))
            or identities.intersection(map(_existing_vault_identity, entry.paths))))


def path_key(path: str) -> str:
    return os.path.normcase(canonical(path))


def vault_identity(path: str) -> tuple:
    value = identity(assert_plain_chain(path, leaf_snapshotter=snapshot))
    if value is None or value[0] != "dir" or not value[2]:
        raise OSError("无法核验仓库目录身份。")
    return value


def _existing_vault_identity(path: str) -> tuple | None:
    try:
        return vault_identity(path)
    except (OSError, SyncError):
        return None


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("已取消识别当前运行仓库。")


def _process_candidates(kernel):
    """Identify the Obsidian process candidates in this interactive Windows session."""
    from ctypes import wintypes as w

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                    ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG),
                    ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = w.BOOL
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = w.BOOL
    kernel.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
    kernel.ProcessIdToSessionId.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    current_session = w.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(current_session)):
        raise ctypes.WinError(ctypes.get_last_error())
    handle = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    pids = set()
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while available:
            if entry.szExeFile.casefold() == "obsidian.exe":
                session = w.DWORD()
                if kernel.ProcessIdToSessionId(entry.th32ProcessID, ctypes.byref(session)):
                    if session.value == current_session.value:
                        pids.add(entry.th32ProcessID)
                elif ctypes.get_last_error() != 87:  # Process exited after snapshot.
                    raise ctypes.WinError(ctypes.get_last_error())
            available = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)
    return pids


class _WindowsAPI:
    def __init__(self):
        from ctypes import wintypes as w
        self.user = user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enum_callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        user.EnumWindows.argtypes = [self.enum_callback, w.LPARAM]
        user.EnumWindows.restype = w.BOOL
        user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        user.GetWindowThreadProcessId.restype = w.DWORD
        user.GetWindow.argtypes = [w.HWND, w.UINT]
        user.GetWindow.restype = w.HWND
        user.GetWindowTextLengthW.argtypes = [w.HWND]
        user.GetWindowTextLengthW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        user.GetWindowTextW.restype = ctypes.c_int
        user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        user.GetClassNameW.restype = ctypes.c_int
        user.IsWindowVisible.argtypes = [w.HWND]
        user.IsWindowVisible.restype = w.BOOL
        user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.PostMessageW.restype = w.BOOL
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = w.BOOL
        kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        kernel.GetProcessTimes.restype = w.BOOL

    def owner_ids(self, hwnd):
        from ctypes import wintypes as w
        pid = w.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value, thread

    def process_identity(self, pid):
        from ctypes import wintypes as w
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = w.DWORD(len(buffer))
            started, exited, kernel_time, user_time = (w.FILETIME() for _ in range(4))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in
                                                         (started, exited, kernel_time, user_time))):
                raise ctypes.WinError(ctypes.get_last_error())
            if ntpath.basename(buffer.value).casefold() != "obsidian.exe":
                raise OSError("Obsidian 进程身份发生变化。")
            return buffer.value, (started.dwHighDateTime << 32) | started.dwLowDateTime
        finally:
            self.kernel.CloseHandle(handle)

    def inspect(self, hwnd, pid, thread, process):
        cls = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, cls, len(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        # A title-less or hidden browser window can still host a vault.
        length = self.user.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(max(length + 1, 256))
        self.user.GetWindowTextW(hwnd, title, len(title))
        owner = self.user.GetWindow(hwnd, 4) or 0  # GW_OWNER
        visible = bool(self.user.IsWindowVisible(hwnd))
        if self.owner_ids(hwnd) != (pid, thread):
            raise OSError("窗口句柄的进程或线程在检查期间改变。")
        exe, started = process
        return ObsidianWindow(hwnd, pid, title.value, exe, started, thread, cls.value, owner, visible)


def _auxiliary(window: ObsidianWindow) -> bool:
    # These exact OS classes belong to the text-input and power facilities,
    # which may be hosted in the Obsidian process even on a private desktop.
    if window.window_class in {
        "IME", "MSCTFIME UI", "Base_PowerMessageWindow", "CicLoaderWndClass",
        "CiceroUIWndFrame", "Touch Tooltip Window", "UAC_InputIndicatorOverlayWnd",
        "UAC Input Indicator",
    }:
        return True
    return not window.visible and (
        (window.window_class == "Chrome_WidgetWin_0" and not window.title)
        or (re.fullmatch(r"temp_d3d_window_\d+", window.window_class) and window.title == "Temp Window"))


def obsidian_windows() -> tuple[ObsidianWindow, ...]:
    """Enumerate potential vault windows, including hidden and minimized ones."""
    if os.name != "nt":
        raise OSError("当前运行仓库识别仅支持 Windows。")
    api = _WindowsAPI()
    pids = _process_candidates(api.kernel)
    found = []
    processes = {}
    failures = []

    @api.enum_callback
    def visit(hwnd, _):
        hwnd = int(hwnd)
        pid, thread = api.owner_ids(hwnd)
        if pid not in pids:
            return True
        try:
            if pid not in processes:
                processes[pid] = api.process_identity(pid)
            window = api.inspect(hwnd, pid, thread, processes[pid])
            if not _auxiliary(window):
                found.append(window)
        except OSError as exc:
            if api.owner_ids(hwnd) != (0, 0):
                failures.append(exc)
        return True

    ctypes.set_last_error(0)
    if not api.user.EnumWindows(visit, 0) and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    if failures:
        raise OSError(f"无法核验部分 Obsidian 窗口：{failures[0]}")
    return tuple(found)


def post_window_close(window: ObsidianWindow) -> None:
    """Send WM_CLOSE to a freshly matched window after a native identity recheck."""
    api = _WindowsAPI()
    pid, thread = api.owner_ids(window.handle)
    if (pid, thread) != (window.process_id, window.thread_id):
        raise OSError("窗口句柄已经消失或被复用。")
    current = api.inspect(window.handle, pid, thread, api.process_identity(pid))
    if current.key != window.key or (current.title, current.owner) != (window.title, window.owner):
        raise OSError("Obsidian 窗口的身份或仓库线索在关闭前改变。")
    if not api.user.PostMessageW(window.handle, 0x0010, 0, 0):  # WM_CLOSE
        raise ctypes.WinError(ctypes.get_last_error())


_TITLE_SUFFIX = re.compile(r" - Obsidian(?: v?\d[^\r\n]*)?$")


def _title_candidates(title, paths):
    name_prefix = _TITLE_SUFFIX.sub("", title)
    if name_prefix == title:
        return None
    name_prefix = os.path.normcase(name_prefix)
    return tuple(path for path in paths if name_prefix == os.path.normcase(Path(path).name)
                 or name_prefix.endswith(" - " + os.path.normcase(Path(path).name)))


def match_runtime_sessions(windows, registered, *, cancel=None, suspect_paths=()) -> RuntimeSnapshot:
    """Preserve full-registrations ambiguity and an explicit unresolved window inventory."""
    _check_cancel(cancel)
    windows = tuple(windows)
    unresolved = [UnresolvedWindow(issue) for issue in registered.issues]
    known = {path_key(path): path for path in registered.paths}
    candidates = dict(known)
    for path in suspect_paths:
        candidates.setdefault(path_key(path), canonical(path))
    matches = {window.handle: _title_candidates(window.title, candidates.values()) for window in windows}
    by_handle = {window.handle: window for window in windows}
    groups = {}
    for window in windows:
        _check_cancel(cancel)
        paths = matches[window.handle]
        # An OS-owned pop-out can inherit its verified top-level vault owner.
        owner = by_handle.get(window.owner)
        if owner and (owner.process_id, owner.process_started) == (window.process_id, window.process_started):
            owner_paths = matches[owner.handle]
            if len(owner_paths or ()) == 1:
                if not paths or owner_paths[0] in paths:
                    paths = owner_paths
                else:
                    unresolved.append(UnresolvedWindow(ManagementIssue(
                        "runtime_owner_conflict", "Obsidian 弹出窗口的标题与其所属窗口指向不同仓库。"),
                        tuple({*paths, *owner_paths}), window))
                    continue
        if len(paths or ()) == 1 and path_key(paths[0]) in known:
            groups.setdefault(paths[0], []).append(window)
        elif paths and len(paths) > 1:
            unresolved.append(UnresolvedWindow(ManagementIssue(
                "runtime_ambiguous", "运行窗口对应多个同名或名称有歧义的仓库。"), paths, window))
        elif paths is None:
            unresolved.append(UnresolvedWindow(ManagementIssue(
                "runtime_title_unknown", "有 Obsidian 窗口无法识别仓库名称。"), window=window))
        else:
            unresolved.append(UnresolvedWindow(ManagementIssue(
                "runtime_unregistered", "有 Obsidian 窗口未匹配到有效登记路径。"), paths, window))

    observed = {}
    windows_by_identity = {}
    for path, matched_windows in groups.items():
        try:
            observed[path] = directory = vault_identity(path)
            windows_by_identity.setdefault(directory, []).extend(matched_windows)
        except (OSError, SyncError) as exc:
            unresolved.append(UnresolvedWindow(ManagementIssue(
                "runtime_identity", f"无法核验运行仓库的目录身份：{exc}", path), (path,)))
    validated = discover_vaults(observed, recursive=False, cancel=cancel)
    unresolved.extend(UnresolvedWindow(issue, (issue.path,) if issue.path and path_key(issue.path) in known else ())
                      for issue in validated.issues)
    sessions = []
    for vault in validated.vaults:
        try:
            directory = vault_identity(vault.path)
            if directory not in windows_by_identity:
                raise OSError("仓库目录在识别期间改变。")
            sessions.append(VaultSession(vault, directory, tuple(windows_by_identity[directory])))
        except (OSError, SyncError) as exc:
            unresolved.append(UnresolvedWindow(ManagementIssue(
                "runtime_identity", f"无法核验运行仓库的目录身份：{exc}", vault.path), (vault.path,)))
    valid_identities = {session.directory_identity for session in sessions}
    for path in (candidate for candidate, directory in observed.items() if directory not in valid_identities):
        unresolved.append(UnresolvedWindow(ManagementIssue(
            "runtime_invalid_vault", "运行窗口的登记路径已失效或不再是仓库。", path), (path,)))
    return RuntimeSnapshot(tuple(sessions), tuple(unresolved), windows=windows)


def match_running_vaults(windows, registered, *, cancel=None) -> VaultCatalogResult:
    return match_runtime_sessions(windows, registered, cancel=cancel).catalog()


def _unavailable(code, message):
    return RuntimeSnapshot(unresolved=(UnresolvedWindow(ManagementIssue(code, message)),))


def read_runtime_snapshot(config_path=None, *, cancel=None, suspect_paths=()) -> RuntimeSnapshot:
    _check_cancel(cancel)
    suspect_paths = tuple(suspect_paths)
    try:
        windows = obsidian_windows()
        if not windows:
            windows = obsidian_windows()
            if not windows:
                return RuntimeSnapshot(notices=(ManagementIssue(
                    "runtime_not_found", "未发现正在运行的 Obsidian 仓库窗口。", severity="warning"),),
                    windows=())
        registered = read_registered_vault_candidates(config_path)
        result = match_runtime_sessions(windows, registered, cancel=cancel, suspect_paths=suspect_paths)
        # Changing a note title in a known vault need not invalidate other vaults.
        anchors = tuple(registered.paths) + tuple(suspect_paths)
        current = obsidian_windows()
        before = {(window.key, window.owner, _title_candidates(window.title, anchors)) for window in windows}
        after = {(window.key, window.owner, _title_candidates(window.title, anchors)) for window in current}
        if before != after or read_registered_vault_candidates(config_path) != registered:
            return _unavailable("runtime_changed", "Obsidian 窗口在识别期间改变，请重新识别。")
        return result
    except (OSError, SyncError) as exc:
        return _unavailable("runtime_unavailable", str(exc))


def read_running_vaults(config_path=None, *, cancel=None) -> VaultCatalogResult:
    return read_runtime_snapshot(config_path, cancel=cancel).catalog()
