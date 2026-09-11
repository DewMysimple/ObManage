"""Render a real ObManage page with synthetic inputs, without scanning a vault."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from obmanage.models import PlanItem, SyncPlan
from obmanage.pages.registry import FEATURES
from obmanage.settings import AppSettings, SettingsStore
from obmanage.ui import MainWindow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "images" / "main-window.png")
    parser.add_argument("--width", type=int, default=1320)
    parser.add_argument("--height", type=int, default=880)
    parser.add_argument("--maximized", action="store_true")
    parser.add_argument(
        "--page", choices=[feature.key for feature in FEATURES], default="mirror",
        help="page to render (default: mirror)",
    )
    args = parser.parse_args()
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    with tempfile.TemporaryDirectory(prefix="obmanage-demo-") as temporary:
        settings = AppSettings()
        settings.set_endpoints(r"C:\Obsidian\日常仓库", r"H:\ObsidianVault\日常仓库", "to_local")
        SettingsStore(temporary).save(settings)
        window = MainWindow(Path(temporary))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, not args.maximized)
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        window.resize(args.width, args.height)
        items = [
            PlanItem("add", "日记/2026-09-10.md", 4380, "目标中不存在"),
            PlanItem("add", "学习笔记/图像处理.md", 15280, "目标中不存在"),
            PlanItem("add", "附件/阅读摘要.pdf", 1820000, "目标中不存在"),
            PlanItem("mkdir", "项目/旅行计划", reason="目标中不存在"),
            PlanItem("add", "项目/旅行计划/行程.md", 6720, "目标中不存在"),
            PlanItem("update", "知识库/摄影/构图技巧.md", 19480, "内容不同，以源端为准"),
            PlanItem("update", ".obsidian/workspace.json", 8100, "内容不同，以源端为准"),
            PlanItem("delete", "收集箱/旧草稿.md", 2100, "来源中不存在"),
            PlanItem("delete", "附件/旧封面.png", 320000, "来源中不存在"),
            PlanItem("rmdir", "归档/空项目", reason="来源中不存在"),
            *[PlanItem("skip", f"归档/笔记{index:04d}.md", 4096, "两端与上次校验记录一致")
              for index in range(1250)],
            PlanItem("skip", "视频/旅行记录.mp4", 2_800_000_000, "两端与上次校验记录一致"),
        ]
        if args.page == "mirror":
            window._accept_plan(SyncPlan(settings.source, settings.target, items), scheduled=False)
        else:
            window._show_page(args.page, persist=False)
        # Only presentation is exercised: no analyze/execute worker is started.
        if args.maximized:
            # The first Windows show may honor the launcher's STARTUPINFO;
            # request maximization after the native window has been created.
            window.show()
            app.processEvents()
            window.showMaximized()
        else:
            window.show()
        for _ in range(15):
            app.processEvents()
            time.sleep(.01)
        assert not window.busy and not window.confirm_direction_checkbox.isChecked()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(args.output)), "Could not save demo screenshot"
        table = window.table if args.page == "mirror" else None
        details = (
            f" viewport={table.viewport().width()} columns={table.horizontalHeader().length()} "
            f"horizontal_scroll={table.horizontalScrollBar().maximum()}"
            if table is not None else ""
        )
        print(f"page={args.page} window={window.width()}x{window.height()}{details}")
        window._timer.stop()
        window._save_timer.stop()
        window.tray.hide()
        window.close()


if __name__ == "__main__":
    main()
