from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect
from PySide6.QtWidgets import QApplication

import obmanage.tasking as tasking_module
from obmanage.engine import SYNC_MODE_NO_VIDEO
from obmanage.models import PlanItem, SyncPlan
from obmanage.pages.backup import VaultBackupPage
from obmanage.pages.common import FeaturePage
from obmanage.settings import SettingsStore
from obmanage.tasking import FeatureWorker
from obmanage.ui import MainWindow


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


def wait_until(app, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    assert condition()


def settle(app):
    deadline = time.monotonic() + 0.08
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def dispose(app, window):
    if window.busy:
        window.cancel_operation()
        wait_until(app, lambda: not window.busy)
    window._timer.stop()
    window._save_timer.stop()
    window.tray.hide()
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def put(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_feature_worker_coalesces_rapid_copy_phase_transitions(
    application, monkeypatch
):
    timestamps = iter((100.0, 100.01, 100.02, 100.11, 100.12))
    monkeypatch.setattr(tasking_module.time, "monotonic", lambda: next(timestamps))
    worker = FeatureWorker(lambda cancel, progress: None)
    received = []
    worker.progress.connect(received.append)
    try:
        for phase in ("copy", "hash", "copied", "copy", "done"):
            worker._progress(type("Event", (), {"phase": phase})())

        assert [event.phase for event in received] == ["copy", "copy", "done"]
    finally:
        worker.deleteLater()


def test_feature_status_skips_identical_text_and_style_redraws(application):
    page = FeaturePage("测试", "状态去重")
    original = page.status_label.setText
    writes = []

    def record(text):
        writes.append(text)
        original(text)

    page.status_label.setText = record
    page.set_status("正在复制并校验", "busy")
    page.set_status("正在复制并校验", "busy")
    page.set_status("正在复制并校验", "success")

    assert writes == ["正在复制并校验", "正在复制并校验"]


def test_shell_registers_backup_page_with_requested_laptop_default(application, tmp_path):
    window = MainWindow(tmp_path / "state")
    try:
        keys = tuple(window.pages)
        assert keys[:4] == ("mirror", "incremental", "vault_backup", "statistics")
        page = window.pages["vault_backup"]
        assert isinstance(page, VaultBackupPage)
        assert Path(page.local_picker.value) == Path.home() / "Desktop" / "ObsidianTest"
        assert page.direction == "to_local"
        assert "视频不会复制、覆盖或删除" in page.findChildren(
            type(page.status_label)
        )[1].text()
    finally:
        dispose(application, window)


def test_backup_page_round_trip_moves_non_video_edits_and_preserves_videos(
    application, tmp_path
):
    portable = tmp_path / "移动硬盘完整仓库"
    laptop = tmp_path / "ObsidianTest"
    portable.mkdir()
    laptop.mkdir()
    put(portable, ".obsidian/app.json", b'{"theme":"dark"}')
    put(portable, "日记.md", b"portable version")
    portable_video = put(portable, "视频/课程.mp4", b"large portable video")
    laptop_video = put(laptop, "本机视频/草稿.mov", b"keep laptop video")
    window = MainWindow(tmp_path / "state")
    page: VaultBackupPage = window.pages["vault_backup"]
    try:
        window._show_page("vault_backup")
        page.local_picker.set_value(str(laptop))
        page.portable_picker.set_value(str(portable))
        page.set_direction("to_local")
        page._start_analyze(False)
        wait_until(application, lambda: not window.busy)

        assert page.plan is not None and page.plan.can_execute
        assert page.plan.counts["exclude"] == 2
        assert {item.action for item in page.model.rows} >= {"add", "exclude"}
        assert "排除 2 个视频" in page.status_label.text()
        page.confirm_checkbox.setChecked(True)
        assert page.execute_button.isEnabled()
        page._start_execute()
        wait_until(application, lambda: not window.busy)

        assert (laptop / "日记.md").read_bytes() == b"portable version"
        assert (laptop / ".obsidian/app.json").read_bytes() == b'{"theme":"dark"}'
        assert not (laptop / "视频/课程.mp4").exists()
        assert laptop_video.read_bytes() == b"keep laptop video"

        put(laptop, "日记.md", b"edited on laptop")
        put(laptop, "新增笔记.md", b"new laptop note")
        (laptop / ".obsidian/app.json").unlink()
        portable_video_before = (
            portable_video.stat().st_ino,
            portable_video.stat().st_mtime_ns,
            portable_video.read_bytes(),
        )
        page.set_direction("to_portable")
        page._start_analyze(False)
        wait_until(application, lambda: not window.busy)
        assert page.plan is not None
        assert page.plan.counts["update"] == 1
        assert page.plan.counts["delete"] == 1
        page.confirm_checkbox.setChecked(True)
        page._start_execute()
        wait_until(application, lambda: not window.busy)

        assert (portable / "日记.md").read_bytes() == b"edited on laptop"
        assert (portable / "新增笔记.md").read_bytes() == b"new laptop note"
        assert not (portable / ".obsidian/app.json").exists()
        assert (
            portable_video.stat().st_ino,
            portable_video.stat().st_mtime_ns,
            portable_video.read_bytes(),
        ) == portable_video_before
        assert "所有视频保持原样" in page.status_label.text()
    finally:
        dispose(application, window)


def test_backup_settings_persist_and_leaving_page_revokes_confirmation(
    application, tmp_path
):
    state = tmp_path / "state"
    local = tmp_path / "笔记本"
    portable = tmp_path / "移动盘"
    local.mkdir()
    portable.mkdir()
    window = MainWindow(state)
    page: VaultBackupPage = window.pages["vault_backup"]
    try:
        page.local_picker.set_value(str(local))
        page.portable_picker.set_value(str(portable))
        page.set_direction("to_portable")
        source, target = page._paths()
        page._accept_plan(SyncPlan(
            source,
            target,
            [PlanItem("add", "note.md", 4, "目标中不存在")],
            mode=SYNC_MODE_NO_VIDEO,
        ))
        window._show_page("vault_backup")
        page.confirm_checkbox.setChecked(True)
        window._show_page("statistics")

        assert page.plan is not None
        assert not page.confirm_checkbox.isChecked()
        assert window._persist()
        settings = SettingsStore(state).load_document().features["vault_backup"]
        assert settings == {
            "local_path": str(local),
            "portable_path": str(portable),
            "direction": "to_portable",
        }
    finally:
        dispose(application, window)


def test_backup_rejects_state_directory_inside_configured_vault(
    application, tmp_path
):
    local = tmp_path / "笔记本仓库"
    portable = tmp_path / "移动硬盘仓库"
    state = local / "app-state"
    local.mkdir()
    portable.mkdir()
    window = MainWindow(state)
    page: VaultBackupPage = window.pages["vault_backup"]
    try:
        page.local_picker.set_value(str(local))
        page.portable_picker.set_value(str(portable))
        page.set_direction("to_local")

        page._start_analyze(False)

        assert not window.busy
        assert not page._task_active
        assert "程序数据目录" in page.status_label.text()
        assert not state.exists()
    finally:
        dispose(application, window)


@pytest.mark.parametrize("size", [(980, 620), (1140, 920), (2560, 1440)])
def test_backup_preview_fits_all_supported_window_sizes(application, tmp_path, size):
    state = tmp_path / f"state-{size[0]}-{size[1]}"
    local = tmp_path / "笔记本轻量仓库"
    portable = tmp_path / "移动硬盘完整仓库"
    local.mkdir()
    portable.mkdir()
    window = MainWindow(state)
    page: VaultBackupPage = window.pages["vault_backup"]
    page.local_picker.set_value(str(local))
    page.portable_picker.set_value(str(portable))
    page.set_direction("to_local")
    source, target = page._paths()
    long_folder = "/".join(["很长的课程目录与附件分类"] * 12)
    rows = [
        PlanItem(action, f"{long_folder}/文件-{index:03d}.{extension}", 128_000, reason)
        for index, (action, extension, reason) in enumerate((
            ("add", "md", "目标中不存在"),
            ("update", "canvas", "内容不同，以源端为准"),
            ("delete", "pdf", "源端已不存在"),
            ("exclude", "mp4", "目标视频已排除，将原样保留，不会删除"),
            ("skip", "png", "两端与上次校验记录一致"),
        ) * 8)
    ]
    page._accept_plan(SyncPlan(
        source,
        target,
        rows,
        mode=SYNC_MODE_NO_VIDEO,
        excluded_source_files=8,
        excluded_source_bytes=1_024_000,
    ))
    window.resize(*size)
    window._show_page("vault_backup", persist=False)
    window.show()
    try:
        settle(application)
        scroll = window.page_containers["vault_backup"]
        assert scroll.horizontalScrollBar().maximum() == 0
        assert page.table.horizontalScrollBar().maximum() == 0
        header = page.table.horizontalHeader()
        assert abs(header.length() - page.table.viewport().width()) <= 1
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        settle(application)
        viewport = scroll.viewport()
        for control in (page.confirm_checkbox, page.execute_button):
            origin = control.mapTo(viewport, QPoint(0, 0))
            assert viewport.rect().intersects(QRect(origin, control.size()))
    finally:
        dispose(application, window)
