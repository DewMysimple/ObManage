"""Time the native chooser and worker, scoped to a collection and empty registry."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import time
from contextlib import ExitStack, nullcontext
from dataclasses import asdict
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget

from obmanage.paths import native
from tools.benchmark_scanning import memory_sample, observations

_APP = None


def measure_picker(modules, root, scratch, cancel, args):
    global _APP
    if os.environ.get("QT_QPA_PLATFORM", "windows").partition(":")[0] != "windows":
        return dict(status="invalid", error_count_by_class={"native_gui_unavailable": 1}), None
    app = _APP = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    if app.platformName() != "windows":
        return dict(status="invalid", error_count_by_class={"native_gui_unavailable": 1}), None
    package = modules["paths"].__package__
    picker = importlib.import_module(f"{package}.pages.vault_selection")
    worker_type = importlib.import_module(f"{package}.tasking").FeatureWorker
    config = scratch / "empty-registry.json"
    if not config.exists():
        with open(native(config), "x", encoding="utf-8") as handle:
            handle.write('{"vaults": {}}')
    parent = QWidget()
    parent.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    parent.show()
    accepted = []
    selection = picker.VaultSelection(parent, lambda path: accepted.append((path, time.perf_counter())),
                                      lambda: None, roots=lambda: (root,))
    selection.config_path = config
    worker = first_paint = None
    painted_history = False
    outcome = []
    metrics = modules["metrics"].ScanMetrics("picker")
    detail = args.counts or args.memory

    def start(operation):
        nonlocal worker

        def background(stop, progress):
            with ExitStack() as stack:
                if detail:
                    stack.enter_context(metrics.activate())
                if args.counts:
                    stack.enter_context(observations(modules, metrics))
                    for name, phase in (("_scope_identity", "picker_scope"),
                                        ("_directory_binding", "picker_bindings"),
                                        ("read_registered_vault_candidates", "picker_registry")):
                        function = getattr(picker, name, None)
                        if function is not None:
                            wrapped = modules["metrics"].measured(phase, calls=True)(function)
                            stack.enter_context(patch.object(picker, name, wrapped))
                return operation(stop, progress)

        worker = worker_type(background)
        worker.progress.connect(selection.task_progress)
        worker.completed.connect(outcome.append, Qt.ConnectionType.DirectConnection)
        worker.start()

    class PaintObserver(QObject):
        def eventFilter(self, watched, event):
            nonlocal first_paint, painted_history
            dialog = selection.dialog
            if (first_paint is None and dialog is not None and event.type() == QEvent.Type.Paint
                    and watched is dialog.table.viewport() and dialog.proxy.rowCount()):
                first_paint = time.perf_counter()
                painted_history = dialog._deferred
            return False

    observer = PaintObserver()
    app.installEventFilter(observer)
    selection.task_requested.connect(start)
    selection.cancel_requested.connect(lambda: worker.request_cancel())
    row = None
    shapes = []
    try:
        for historical in (False, True):
            first_paint = None
            painted_history = False
            outcome.clear()
            accepted.clear()
            with memory_sample(args.memory and historical, metrics) if detail else nullcontext():
                started = time.perf_counter()
                selection.start()
                while worker.isRunning() or not outcome:
                    app.processEvents()
                    if cancel.is_set():
                        worker.request_cancel()
                    time.sleep(0.001)
                    if historical and first_paint is not None and selection._pending_path is None:
                        selection.dialog.table.selectRow(0)
                        selection.dialog._choose()
                worker.wait()
                status, payload = outcome[0]
                if status != "ok" or payload.choices.issues or payload.display is None or not payload.choices.vaults:
                    errors = ({issue.code for issue in payload.choices.issues} if status == "ok" else {status})
                    return dict(status="invalid", error_count_by_class=dict.fromkeys(errors or {"no_verified_rows"}, 1)), None
                queued_history_selection = selection._pending_path is not None
                selection.task_finished(status, payload)
                ready = time.perf_counter()
                app.processEvents()
                if historical and not (painted_history and queued_history_selection and accepted):
                    return dict(status="invalid", error_count_by_class={"historical_choice_not_verified": 1}), None
                if not historical:
                    selection.dialog.table.selectRow(0)
                    selection.dialog._choose()
                if first_paint is None or not accepted:
                    return dict(status="invalid", error_count_by_class={"picker_no_paint_or_selection": 1}), None
                shapes.append((asdict(payload), accepted[0][0]))
                timing = dict(first_visible_ms=(first_paint - started) * 1000,
                              verified_ready_ms=(ready - started) * 1000,
                              selection_accepted_ms=(accepted[0][1] - started) * 1000,
                              painted_history=painted_history, queued_history_selection=queued_history_selection)
                if not historical:
                    row = dict(cold=timing)
                else:
                    row.update(timing, total_wall_ms=timing["verified_ready_ms"],
                               status="valid", error_count_by_class={}, vault_count=len(payload.choices.vaults))
        if detail:
            row["counts"] = metrics.report()["counts"]
            row["phase_ms"] = {key: value * 1000 for key, value in metrics.report()["seconds"].items()}
        return row, hashlib.sha256(json.dumps(shapes, sort_keys=True).encode("ascii")).digest()
    finally:
        if worker is not None and worker.isRunning():
            worker.request_cancel()
            worker.wait()
        selection.dismiss()
        app.removeEventFilter(observer)
        parent.close()
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
