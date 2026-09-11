from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .settings import AppSettings, default_state_dir
from . import __version__


def setup_logging(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(state_dir / "obmanage.log", maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def acquire_instance_lock(state_dir: Path):
    """Serialize GUI and CLI analysis against the shared baseline database."""
    from PySide6.QtCore import QLockFile

    lock = QLockFile(str(state_dir / "instance.lock"))
    lock.setStaleLockTime(0)
    return lock if lock.tryLock(0) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ObManage — Obsidian 单向增量镜像")
    parser.add_argument("--state-dir", type=Path, help="设置和同步记录目录")
    parser.add_argument("--preview", action="store_true", help="只分析差异，不写入源目录或目标目录")
    parser.add_argument("--source", default=AppSettings.source)
    parser.add_argument("--target", default=AppSettings.target)
    parser.add_argument("--deep", action="store_true", help="分析时完整校验文件内容")
    parser.add_argument("--smoke-test", type=Path, metavar="PNG", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    temporary_state = tempfile.TemporaryDirectory(prefix="obmanage-ui-") if args.smoke_test and not args.state_dir else None
    state_dir = args.state_dir or (Path(temporary_state.name) if temporary_state else default_state_dir())
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

        lock = acquire_instance_lock(state_dir)
        if lock is None:
            print("ObManage 已有任务运行中，请稍后重试。", file=sys.stderr)
            return 2
        try:
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
            lock.unlock()

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
        QMessageBox.information(None, "ObManage 已在运行", "请从右下角系统托盘打开已有的 ObManage 窗口。")
        return 0

    def report_exception(kind, value, traceback):
        logging.critical("未处理的程序错误", exc_info=(kind, value, traceback))
        QMessageBox.critical(None, "ObManage", f"发生错误：{value}\n详细信息已保存在运行日志。")

    sys.excepthook = report_exception
    from .ui import MainWindow

    logging.info("Creating main window")
    window = MainWindow(state_dir)
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
        lock.unlock()
        logging.shutdown()
        if temporary_state:
            temporary_state.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
