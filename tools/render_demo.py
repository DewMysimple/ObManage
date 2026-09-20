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

from obmanage.engine import SYNC_MODE_NO_VIDEO
from obmanage.models import PlanItem, SyncPlan
from obmanage.management.incremental import IncrementalAnalysis, VaultPair
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
        elif args.page == "incremental":
            page = window.pages["incremental"]
            local, portable = r"C:\Obsidian\仓库集合", r"H:\ObsidianVault\仓库集合"
            page.local_picker.set_value(local)
            page.portable_picker.set_value(portable)
            pairs = []
            for key, label in (("a", "工作/A仓库"), ("b", "学习/B仓库"), ("c", "资料/C仓库")):
                left, right = str(Path(local) / label), str(Path(portable) / label)
                forward = SyncPlan(left, right, [
                    PlanItem("add", "笔记/电脑新稿.md", 4380, "目标中不存在"),
                    PlanItem("update", "笔记/项目进度.md", 15280, "内容不同，以源端为准"),
                    PlanItem("delete", "归档/旧草稿.md", 2100, "源端已不存在"),
                ])
                backward = SyncPlan(right, left, [
                    PlanItem("add", "归档/旧草稿.md", 2100, "目标中不存在"),
                    PlanItem("update", "笔记/项目进度.md", 19480, "内容不同，以源端为准"),
                    PlanItem("delete", "笔记/电脑新稿.md", 4380, "源端已不存在"),
                ])
                pairs.append(VaultPair(key, label, left, right, forward, backward))
            page._accept_analysis(IncrementalAnalysis(local, portable, tuple(pairs), 6))
            for key, side in (("a", "portable"), ("b", "local")):
                selector = page.source_selectors[key]
                selector.setCurrentIndex(selector.findData(side))
            page.vault_table.selectRow(0)
            window._show_page(args.page, persist=False)
        elif args.page == "vault_backup":
            page = window.pages["vault_backup"]
            page.local_picker.set_value(r"C:\Obsidian\ObsidianTest")
            page.portable_picker.set_value(r"H:\ObsidianVault\完整仓库")
            page.set_direction("to_local")
            source, target = page._paths()
            backup_items = [
                item for item in items if not item.relative_path.casefold().endswith(".mp4")
            ]
            backup_items.extend((
                PlanItem("exclude", "视频/旅行记录.mp4", 2_800_000_000,
                         "视频已排除；两端现有文件均保持原样，不比较内容"),
                PlanItem("exclude", "视频/课程录像.mkv", 4_600_000_000,
                         "来源视频已排除，不会复制到目标"),
            ))
            page._accept_plan(SyncPlan(
                source,
                target,
                backup_items,
                mode=SYNC_MODE_NO_VIDEO,
                excluded_source_files=2,
                excluded_source_bytes=7_400_000_000,
                excluded_target_files=1,
                excluded_target_bytes=2_800_000_000,
            ))
            window._show_page(args.page, persist=False)
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
        table = (
            window.table
            if args.page == "mirror"
            else window.pages[args.page].table
            if args.page in {"vault_backup", "incremental"}
            else None
        )
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
