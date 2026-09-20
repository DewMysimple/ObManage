from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .models import SyncError
from .paths import validate_state_separation
from .settings import AppSettings, SettingsStore, default_state_dir
from . import __version__


def setup_logging(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(state_dir / "obmanage.log", maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    setattr(handler, "_obmanage_owned", True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def _shutdown_logging() -> None:
    """Close and detach app-owned handlers so embedded preview calls cannot reopen them."""
    root = logging.getLogger()
    for handler in tuple(root.handlers):
        if getattr(handler, "_obmanage_owned", False):
            root.removeHandler(handler)
            handler.close()
    logging.shutdown()


def acquire_instance_lock(state_dir: Path):
    """Keep repository-management tasks inside one application instance."""
    from PySide6.QtCore import QLockFile

    lock = QLockFile(str(state_dir / "instance.lock"))
    lock.setStaleLockTime(0)
    return lock if lock.tryLock(0) else None


def instance_focus_server_name(state_dir: Path) -> str:
    """Return a stable, short local IPC name for one application state tree."""
    normalized = os.path.normcase(os.path.abspath(os.fspath(state_dir)))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"ObManage-{digest}"


def notify_existing_instance(state_dir: Path) -> bool:
    """Ask the running instance for this state tree to focus its main window."""
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(instance_focus_server_name(state_dir))
    if not socket.waitForConnected(300):
        return False
    socket.write(b"focus")
    socket.waitForBytesWritten(300)
    socket.disconnectFromServer()
    return True


def focus_main_window(window: object) -> None:
    """Restore and activate the GUI window, including Windows foreground rules."""
    show_window = getattr(window, "_show_window")
    show_window()
    if os.name != "nt":
        return
    try:
        hwnd = int(window.winId())
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND

        foreground = user32.GetForegroundWindow()
        current_thread = user32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(foreground_thread and foreground_thread != current_thread
                        and user32.AttachThreadInput(foreground_thread, current_thread, True))
        try:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(foreground_thread, current_thread, False)
    except (AttributeError, OSError, TypeError, ValueError):
        logging.getLogger(__name__).debug("Windows 前台窗口激活兜底失败", exc_info=True)


def create_instance_focus_server(state_dir: Path, on_focus: Callable[[], None]):
    """Listen for second-launch requests and invoke ``on_focus`` in the GUI thread."""
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer()
    name = instance_focus_server_name(state_dir)

    # A crashed GUI can leave a local-server endpoint behind even though its
    # QLockFile is no longer held.  The instance lock has already established
    # ownership before this function is called, so removing that stale endpoint
    # is safe here.
    if not server.listen(name):
        QLocalServer.removeServer(name)
        if not server.listen(name):
            logging.getLogger(__name__).warning(
                "无法创建重复启动聚焦通道 %s: %s", name, server.errorString()
            )
            return None

    def handle_connections() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            if socket is None:
                continue
            on_focus()
            socket.disconnectFromServer()
            socket.deleteLater()

    server.newConnection.connect(handle_connections)
    return server


def _configured_repository_paths(state_dir: Path, *, preview: bool,
                                 source: str, target: str) -> tuple[str, ...]:
    """Read startup paths without creating settings, logs, locks, or journals."""
    if preview:
        return source, target
    document = SettingsStore(state_dir).load_document()
    paths = [document.mirror.source, document.mirror.target]
    for settings in document.features.values():
        if not isinstance(settings, dict):
            continue
        for field in ("root", "source", "local_path", "portable_path"):
            value = settings.get(field)
            if isinstance(value, str) and value.strip():
                paths.append(value)
    return tuple(paths)


def _report_startup_rejection(message: str, *, gui: bool) -> None:
    print(message, file=sys.stderr)
    if gui and os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "ObManage 启动已拒绝", 0x10)
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ObManage — Obsidian 仓库管理")
    parser.add_argument("--state-dir", type=Path, help="设置、任务记录和恢复记录目录")
    parser.add_argument("--preview", action="store_true", help="只分析仓库镜像差异，不写入源目录或目标目录")
    parser.add_argument("--source", default=AppSettings.source)
    parser.add_argument("--target", default=AppSettings.target)
    parser.add_argument("--deep", action="store_true", help="分析时完整校验文件内容")
    parser.add_argument("--smoke-test", type=Path, metavar="PNG", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    temporary_state = tempfile.TemporaryDirectory(prefix="obmanage-ui-") if args.smoke_test and not args.state_dir else None
    state_dir = args.state_dir or (Path(temporary_state.name) if temporary_state else default_state_dir())
    try:
        roots = _configured_repository_paths(
            state_dir, preview=args.preview, source=args.source, target=args.target
        )
        state_dir = Path(validate_state_separation(state_dir, roots))
    except (OSError, SyncError, ValueError) as exc:
        _report_startup_rejection(str(exc), gui=not args.preview)
        if temporary_state:
            temporary_state.cleanup()
        return 2
    setup_logging(state_dir)
    logging.info("ObManage starting; frozen=%s; preview=%s; smoke=%s", getattr(sys, "frozen", False), args.preview, bool(args.smoke_test))
    if args.preview:
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        from .engine import SyncEngine

        last_message = 0.0

        def progress(event):
            nonlocal last_message
            if time.monotonic() - last_message >= 5:
                print(json.dumps({"phase": event.phase, "files": event.completed_files, "total_files": event.total_files, "bytes": event.completed_bytes}, ensure_ascii=False), flush=True)
                last_message = time.monotonic()

        lock = None
        try:
            lock = acquire_instance_lock(state_dir)
            if lock is None:
                print("ObManage 已有任务运行中，请稍后重试。", file=sys.stderr)
                return 2
            plan = SyncEngine(state_dir).analyze(args.source, args.target, deep=args.deep, progress=progress)
            print(json.dumps({"source": plan.source, "target": plan.target, "counts": plan.counts,
                              "bytes_to_copy": plan.bytes_to_copy, "errors": plan.errors,
                              "changes": [asdict(item) for item in plan.items if item.action != "skip"]}, ensure_ascii=False, indent=2))
            return 0 if plan.can_execute else 2
        except Exception as exc:
            logging.exception("差异预览失败")
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            if lock is not None:
                lock.unlock()
            _shutdown_logging()
            if temporary_state:
                temporary_state.cleanup()

    logging.info("Loading Qt modules")
    from PySide6.QtCore import QTimer, qInstallMessageHandler
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QMessageBox

    def qt_message(kind, context, message):
        logging.getLogger("Qt").warning("%s", message)

    qInstallMessageHandler(qt_message)
    logging.info("Creating QApplication")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ObManage")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("ObManage")
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    lock = acquire_instance_lock(state_dir)
    if lock is None:
        if not notify_existing_instance(state_dir):
            QMessageBox.information(None, "ObManage 已在运行", "请从右下角系统托盘打开已有的 ObManage 窗口。")
        _shutdown_logging()
        if temporary_state:
            temporary_state.cleanup()
        return 0

    def report_exception(kind, value, traceback):
        logging.critical("未处理的程序错误", exc_info=(kind, value, traceback))
        QMessageBox.critical(None, "ObManage", f"发生错误：{value}\n详细信息已保存在运行日志。")

    sys.excepthook = report_exception
    from .ui import MainWindow

    window_holder: list[MainWindow | None] = [None]

    def focus_window() -> None:
        window = window_holder[0]
        if window is not None:
            focus_main_window(window)

    focus_server = create_instance_focus_server(state_dir, focus_window)
    logging.info("Creating main window")
    window = MainWindow(state_dir)
    window_holder[0] = window
    window.show()
    logging.info("Main window ready")
    if args.smoke_test:
        def capture_and_exit():
            args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.smoke_test)):
                logging.error("界面截图保存失败：%s", args.smoke_test)
                app.exit(3)
            else:
                app.quit()

        QTimer.singleShot(700, capture_and_exit)
    try:
        return app.exec()
    finally:
        if focus_server is not None:
            focus_server.close()
        lock.unlock()
        _shutdown_logging()
        if temporary_state:
            temporary_state.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
