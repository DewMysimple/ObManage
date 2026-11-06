"""Compare first painted picker rows and live readiness on the same synthetic tree."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.benchmark_sandbox import sandbox
from tools.benchmark_scanning import BASELINE, fixture, reference_package, volume_info, write_report


def measure(app, package, base, root, config, samples, repeat, version):
    selection_module = importlib.import_module(f"{package}.pages.vault_selection")
    ui = importlib.import_module(f"{package}.ui")
    original_dialog = selection_module.VaultSourceDialog
    first_paint = None
    painted_history = False
    heartbeats = 0

    class PaintObserver(QObject):
        def eventFilter(self, watched, event):
            nonlocal first_paint, painted_history
            if first_paint is None and event.type() == QEvent.Type.Paint and isinstance(watched, QWidget):
                dialog = watched.window()
                if (isinstance(dialog, original_dialog) and watched is dialog.table.viewport()
                        and dialog.proxy.rowCount()):
                    first_paint = time.perf_counter()
                    freshness = getattr(dialog, "freshness", None)
                    painted_history = bool(freshness and "上次" in freshness.text())
            return False

    def tick():
        nonlocal heartbeats
        heartbeats += 1

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(5)
    window = ui.MainWindow(base / f"state-{repeat}-{version}")
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.local_edit.setText(str(root))
    window.local_selection.config_path = config
    stats = window.pages["statistics"].root_picker
    stats.set_value(str(root))
    stats.selection.config_path = config
    window.show()
    observer = PaintObserver()
    app.installEventFilter(observer)
    try:
        for scenario, page, selection in (
                ("first", "mirror", window.local_selection),
                ("cross_page", "statistics", stats.selection),
                ("repeated", "statistics", stats.selection)):
            window._show_page(page, persist=False)
            app.processEvents()
            first_paint = ready = None
            painted_history = False
            heartbeats = 0
            original_finished = selection.task_finished

            def finished(*args):
                nonlocal ready
                original_finished(*args)
                assert args[0] == "ok" and not args[1].choices.issues
                ready = time.perf_counter()

            with patch.object(selection, "task_finished", finished):
                started = time.perf_counter()
                selection.start()
                assert window.busy
                while first_paint is None or ready is None:
                    app.processEvents()
                    if time.perf_counter() - started > 60:
                        raise RuntimeError("Picker benchmark did not paint and complete")
                    time.sleep(0.001)
                assert selection.dialog.model.rowCount() > 0
                samples.append(dict(scenario=scenario, version=version, repeat=repeat,
                    ui_first_visible=first_paint - started, ui_verified_ready=ready - started,
                    painted_history=painted_history, gui_timer_ticks=heartbeats,
                    vaults=selection.dialog.model.rowCount()))
                selection.dialog.reject()
    finally:
        app.removeEventFilter(observer)
        timer.stop()
        if window.busy:
            window.cancel_operation()
            while window.busy:
                app.processEvents()
                time.sleep(0.005)
        window._timer.stop()
        window._save_timer.stop()
        window.tray.hide()
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--vaults", type=int, default=24)
    parser.add_argument("--files", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.vaults, args.files, args.repeats) < 1:
        parser.error("counts must be positive")
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    samples = []
    with reference_package(args.baseline_ref), sandbox() as base:
        root, _ = fixture(base, args.vaults, args.files)
        config = base / "obsidian" / "obsidian.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"vaults": {}}), encoding="utf-8")
        with patch.dict(os.environ, {"APPDATA": str(base)}):
            for repeat in range(args.repeats):
                versions = (("before", "scan_reference"), ("after", "obmanage"))
                for version, package in versions if repeat % 2 == 0 else reversed(versions):
                    measure(app, package, base, root, config, samples, repeat, version)
        report = dict(reference=args.baseline_ref, platform=platform.platform(),
                      python=platform.python_version(), qt_platform=app.platformName(),
                      volume=volume_info(root), files_per_vault=args.files, vaults=args.vaults + 1,
                      repeats=args.repeats, cache="OS cache uncontrolled; alternating versions",
                      samples=samples, medians={})
    for scenario in ("first", "cross_page", "repeated"):
        report["medians"][scenario] = {version: {
            key: statistics.median(sample[key] for sample in samples
                if sample["scenario"] == scenario and sample["version"] == version)
            for key in ("ui_first_visible", "ui_verified_ready")}
            for version in ("before", "after")}
    write_report(args.output, report)


if __name__ == "__main__":
    main()
