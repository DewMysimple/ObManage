"""Controls and scheduling shared by the mirror and non-video backup pages."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime

from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from ..engine import SYNC_MODE_MIRROR, SYNC_MODE_NO_VIDEO
from ..models import SyncError
from ..paths import canonical, identity, native, snapshot
from ..realtime import automatic_overlap, enable_realtime, portable_path, reconcile
from ..realtime_store import RealtimeStore
from ..realtime_watch import DirectoryWatch
from ..sync_workflow import SyncIntent, SyncRequest, confirmation_text, sync_input_error
from ..tasking import ConfirmableOperation, TaskConfirmation


QUIET_SECONDS = 2.5
AUDIT_SECONDS = 3600
MAX_PENDING = 4096


class RealtimePanel(QFrame):
    def __init__(self, window, page_key):
        super().__init__()
        self.host = window
        self.page_key = page_key
        self.mode = SYNC_MODE_MIRROR if page_key == "mirror" else SYNC_MODE_NO_VIDEO
        saved = window.settings_document.features.get("realtime", {}).get(page_key, {})
        self.config = dict(saved) if isinstance(saved, dict) else {}
        self.enabled = (self.config.get("enabled") is True and all(
            isinstance(self.config.get(k), str) and self.config[k] for k in ("token", "source", "target")))
        self.watch = None
        self.watch_error = ""
        self.watch_root = None
        self.next_watch = 0.0
        self.next_probe = 0.0
        self.connected = None
        self.active_target = None
        self.locator = None
        self.locator_token = None
        self.next_audit = 0.0
        self.device_changed = False
        self.pending = {"": 0.0} if self.enabled else {}
        self.idle_check = False
        self.issues = {}
        self.retry_delay = 7
        self._task_active = False
        self._task_kind = ""
        self.inflight = None
        self.status = ""
        self.last_success = str(self.config.get("last_success", ""))
        compact = page_key == "mirror"
        if not compact:
            self.setObjectName("Panel")
        layout = QVBoxLayout(self)
        side, top = (0, 0) if compact else (14, 5)
        layout.setContentsMargins(side, top, side, top)
        layout.setSpacing(2)
        row = QHBoxLayout()
        self.title = QLabel("实时同步 · 本机 → 移动硬盘")
        row.addWidget(self.title)
        row.addStretch()
        self.check_button = QPushButton("立即核对并同步")
        self.check_button.setToolTip("核对两端全部受管内容，补齐本机已保存的变化。")
        self.toggle = QPushButton()
        self.toggle.setToolTip("自动将本机已保存的变化同步到移动硬盘，包括已复核的删除。")
        row.addWidget(self.check_button)
        self.rebind = QPushButton("重新绑定")
        self.rebind.setToolTip("重新核对当前位置的卷、目录和完整差异，再确认开启。")
        row.addWidget(self.rebind)
        row.addWidget(self.toggle)
        layout.addLayout(row)
        self.label = QLabel()
        self.label.setObjectName("Muted")
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.label)
        self.toggle.clicked.connect(self.toggle_sync)
        self.rebind.clicked.connect(lambda: self.toggle_sync(rebind=True))
        self.check_button.clicked.connect(self.check_now)
        QApplication.instance().aboutToQuit.connect(self.close_watch)
        self.render()

    def paths(self):
        if self.page_key == "mirror":
            return self.host.local_edit.text().strip(), self.host.portable_combo.currentText().strip()
        page = self.host.pages[self.page_key]
        return page.local_picker.value, page.portable_picker.value

    def direction(self):
        return (self.host.settings.direction if self.page_key == "mirror"
                else self.host.pages[self.page_key].direction)

    def matches(self):
        return self.matches_paths() and self.direction() == "to_portable"

    def matches_paths(self):
        return all(isinstance(saved, str) and saved and value and
                   os.path.normcase(canonical(saved)) == os.path.normcase(canonical(value))
                   for saved, value in zip((self.config.get("source"), self.config.get("target")), self.paths()))

    def persist(self, *, immediate=False):
        self.config.update(enabled=bool(self.enabled), last_success=self.last_success,
                           pending=list(self.pending) if self.inflight is None else [""])
        self.host.settings_document.feature("realtime")[self.page_key] = dict(self.config)
        if immediate:
            return self.host._persist()
        self.host._queue_settings_save()
        return True

    def render(self):
        self.toggle.setText("暂停实时同步" if self.enabled else "开启实时同步")
        self.toggle.setEnabled(not self.host._exit_requested and (self.enabled or not self.host.busy))
        self.check_button.setVisible(bool(self.enabled))
        self.rebind.setVisible(bool(self.config.get("token")) and not self.enabled and not self._task_active)
        self.rebind.setEnabled(not self.host.busy)
        self.check_button.setEnabled(bool(self.enabled) and not self.host._exit_requested)
        if self._task_active:
            state = self.status or "正在核对"
        elif self.enabled and self.connected is False:
            state = "等待移动硬盘连接"
        elif self.issues:
            name, reason = next(iter(self.issues.items()))
            state = "需要处理 · " + (f"{name} · " if name else "") + reason.splitlines()[0]
        elif not self.enabled:
            state = self.status or "已关闭"
        elif self.watch_error:
            state = "等待恢复监听 · " + self.watch_error
        elif self.pending:
            state = "待同步 · 等待全量核对" if "" in self.pending else f"待同步 {len(self.pending)} 项"
        else:
            state = "已同步 · 监听中"
        if self.enabled and self.host._recovery_page_keys() and not self._task_active:
            state = "等待事务处理 · 变化已保留"
        self.summary = state
        if self.enabled and self.pending and not state.startswith("待同步"):
            state += " · 待核对全部" if "" in self.pending else f" · 待同步 {len(self.pending)} 项"
        if self.last_success:
            state += f" · 最近成功 {self.last_success}"
        if self.enabled and self.active_target and os.path.normcase(self.active_target) != os.path.normcase(
                self.config["target"]):
            state += f" · 当前目标 {self.active_target}"
        self.label.setText(state)
        self.title.setVisible(self.page_key != "mirror" or bool(self.enabled))
        self.label.setVisible(self.page_key != "mirror" or bool(self.enabled or self.status or self.issues))
        details = "\n".join(f"{n}  {msg}" for n, msg in self.issues.items()) or state
        if self.active_target:
            details = f"当前移动硬盘目录 · {self.active_target}\n{details}"
        self.label.setToolTip(details)

    def set_status(self, message, kind=""):
        self.status = message
        self.render()

    def refresh_actions(self):
        self.render()

    def task_cancellable(self):
        return self._task_active

    def task_progress(self, event):
        self.set_status(f"同步中 · {event.message or event.relative_path}")

    def task_confirmation(self, prompt):
        return (self._task_kind == "开启实时同步" and self.matches()
                and not self.host._recovery_page_keys() and not self.overlap_error())

    def overlap_error(self):
        source, target = self.paths()
        target = self.active_target or target
        for panel in self.host.live_panels.values():
            if panel is not self and panel.enabled and automatic_overlap(
                    source, target, panel.config["source"], panel.active_target or panel.config["target"]):
                return "与另一页的实时同步读写范围重叠，请先暂停其中一项。"
        settings = self.host.settings
        if settings.schedule_enabled and automatic_overlap(source, target, settings.bound_source, settings.bound_target):
            return "与已开启的定时同步范围重叠，请先暂停定时。"
        return ""

    def other_automatic(self):
        roots = [(p.config["source"], p.config["target"], p.locator)
                 for p in self.host.live_panels.values() if p is not self and p.enabled]
        settings = self.host.settings
        if settings.schedule_enabled:
            roots.append((settings.bound_source, settings.bound_target, None))
        return tuple(roots)

    def toggle_sync(self, checked=False, *, rebind=False):
        if self.enabled:
            self.pause()
            return
        host = self.host
        if host.busy or host._exit_requested or host._reject_unsafe_state_location(self):
            return
        if host._recovery_page_keys():
            self.set_status("请先在事务处理页处理待恢复事务。")
            return
        source, target = self.paths()
        error = sync_input_error(source, target) or self.overlap_error()
        if error:
            self.set_status(error)
            return
        if self.direction() != "to_portable":
            owner = host if self.page_key == "mirror" else host.pages[self.page_key]
            owner.set_direction("to_portable")
        self._previous_config = dict(self.config)
        resume = self.matches_paths() and bool(self.config.get("token")) and not rebind
        self.config.update(source=source, target=target)
        self.issues.clear()
        self.status = ""
        self.connected = None
        if resume:
            self.enabled = True
            self.queue({""}, 0)
            if not self.persist(immediate=True):
                self.pause()
            self.render()
            return
        self.last_success = ""
        self._task_active = True
        self._task_kind = "开启实时同步"
        self.status = "正在核对首次同步范围…"
        automatic = self.other_automatic()
        try:
            self.watch_root = identity(snapshot(source))
            self.watch = DirectoryWatch(source)
            self.watch_error = ""
        except OSError as exc:
            self.watch_error = str(exc)

        def enable(cancel, progress, ask):
            def confirm(plan):
                req = SyncRequest(source, target, "本机", "移动硬盘", self.mode, SyncIntent.MANUAL)
                _, summary, details, approve = confirmation_text(req, plan)
                labels = {"add": "新增", "mkdir": "建目录", "rename": "改名", "update": "更新",
                          "delete": "删除", "rmdir": "删除目录"}
                changes = [f"{labels[i.action]}  {i.relative_path}" for i in plan.items if i.action in labels]
                return ask(TaskConfirmation("开启实时同步", summary + "\n之后自动同步本机已保存的变化。",
                                            "\n".join([details, *changes]),
                                            ("开启并" + approve if plan.source_empty and changes else "开启实时同步")))
            return enable_realtime(host.state_dir, source, target, self.mode, cancel, progress, confirm,
                                   automatic=automatic)

        host._start_feature_task(self, ConfirmableOperation(enable))
        if host._feature_page is not self:
            self.close_watch()
            self.config = self._previous_config

    def pause(self, message="已暂停"):
        self.enabled = False
        if self._task_active and self.host._feature_page is self:
            self.host._feature_worker.request_cancel()
            self.host._dismiss_worker_confirmation()
        self.close_watch()
        self.active_target = None
        self.status = message
        self.issues.clear()
        self.persist(immediate=True)
        self.render()

    def inputs_changed(self):
        if (self.enabled or self._task_active) and not self.matches():
            self.pause("位置或方向已改变，实时同步已暂停。")

    def close_watch(self):
        if self.watch:
            self.watch.close()
            self.watch = None
            self.watch_root = None

    def queue(self, names, delay=QUIET_SECONDS, *, idle=False):
        due = time.monotonic() + delay
        if "" in names or len(self.pending) + len(names) > MAX_PENDING:
            self.idle_check = idle and (not self.pending or (set(self.pending) == {""} and self.idle_check))
            # Reconcile captured notifications first. Later arrivals get a new generation.
            self.pending = {"": min(due, self.pending.get("", due))}
        else:
            if names:
                self.idle_check = False
            self.pending.update(dict.fromkeys(names, due))
        self.persist()

    def check_now(self):
        if self.enabled:
            self.queue({""}, 0)
            self.next_probe = 0
            self.render()

    def observe(self, now):
        if not self.enabled:
            return
        if not self.matches():
            self.pause("位置或方向与绑定不同，实时同步已暂停。")
            return
        source, target = self.paths()
        if self.device_changed:
            self.device_changed = False
            self.queue({""}, 0)
        if now >= self.next_probe:
            self.next_probe = now + 3
            try:
                source_id = identity(snapshot(source))
                if self.watch and source_id != self.watch_root:
                    self.close_watch()
                    self.queue({""}, 0)
                token = self.config["token"]
                if token != self.locator_token:
                    self.locator = RealtimeStore.binding_locator(self.host.state_dir, token, self.mode)
                    self.locator_token = token
                actual = portable_path(target, self.locator)
                present = actual is not None and os.path.isdir(native(actual))
            except (OSError, SyncError, sqlite3.Error, ValueError, TypeError) as exc:
                self.issues[""] = str(exc)
                self.connected = None
                self.render()
                return
            if present != self.connected or actual != self.active_target:
                self.connected = present
                self.active_target = actual
                self.queue({""}, 0)
        if not self.watch and now >= self.next_watch:
            self.next_watch = now + 5
            try:
                self.watch_root = identity(snapshot(source))
                self.watch = DirectoryWatch(source)
                self.watch_error = ""
                self.queue({""}, 0)
            except OSError as exc:
                self.watch_error = str(exc)
        if self.watch:
            try:
                names = self.watch.poll()
                if names:
                    self.queue(names)
            except OSError as exc:
                self.watch_error = str(exc)
                self.close_watch()
                self.queue({""})
        if now >= self.next_audit:
            self.next_audit = now + (AUDIT_SECONDS if self.watch else 60)
            self.queue({""}, 0, idle=not self.issues and self.watch is not None)
        self.render()

    def dispatch(self, now):
        if not self.enabled or not self.connected or not self.pending or self.host.busy:
            return False
        error = self.overlap_error()
        if error:
            self.pause(error)
            return False
        due = {n for n, when in self.pending.items() if when <= now}
        if not due:
            return False
        full = "" in due
        idle = full and self.idle_check
        self.idle_check = False
        self.inflight = due
        for n in self.inflight:
            self.pending.pop(n)
        self._task_active = True
        self._task_kind = "实时同步"
        self.status = "正在核对两端…" if full else "正在同步已保存的变化…"
        source, target = self.paths()
        token = self.config["token"]
        automatic = self.other_automatic()

        def run(cancel, progress):
            return reconcile(self.host.state_dir, source, target, self.mode, token,
                             None if full else due, cancel, progress, idle=idle, automatic=automatic)
        self.host._start_feature_task(self, run)
        if self.host._feature_page is self:
            return True
        pending, self.inflight = self.inflight, None
        self.queue(pending, 3)
        return False

    def task_finished(self, status, value):
        first = self._task_kind == "开启实时同步"
        self._task_active = False
        inflight, self.inflight = self.inflight, None
        if first:
            if status == "ok" and self.matches():
                self.config["token"] = value
                self.enabled = True
                self.pending.clear()
                self.issues.clear()
                # The initial full compare already finished. Notifications have
                # accumulated since before it started, including any overflow.
                self.connected = True
                self.active_target = self.config["target"]
                self.next_audit = time.monotonic() + AUDIT_SECONDS
                self.observe(time.monotonic())
                if not self.watch:
                    self.queue({""}, 0)
                if not self.pending and not self.issues:
                    self.last_success = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.status = ""
                if not self.persist(immediate=True):
                    self.pause("绑定设置保存失败，实时同步已暂停。")
            else:
                self.close_watch()
                self.status = str(value) if status == "error" else "已取消开启实时同步。"
                self.config = self._previous_config
                self.last_success = self.config.get("last_success", "")
        elif self.enabled:
            if not self.host._exit_requested:
                self.observe(time.monotonic())
            if status != "ok":
                self.issues[""] = str(value or "本轮已停止，变化已保留。")
                self.queue(inflight or {""}, 15)
            else:
                previous_issues = dict(self.issues)
                if value.namespace_checked:
                    self.issues.pop("", None)
                covered = value.covered
                if covered is None:
                    self.issues.clear()
                else:
                    for n in list(self.issues):
                        if any(n.casefold() == p.casefold() or n.casefold().startswith(p.casefold() + "/")
                               for p in covered):
                            del self.issues[n]
                self.issues.update(value.issues)
                if value.waiting_disk:
                    self.connected = False
                    self.queue({""}, 3)
                if value.retry:
                    self.retry_delay = (min(60, self.retry_delay * 2) if value.issues
                                        and self.issues == previous_issues else 7)
                    remaining = value.retry.difference(self.pending)
                    if remaining:
                        self.queue(remaining, self.retry_delay)
                if not self.pending and not self.issues:
                    self.last_success = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if value.issues and self.issues != previous_issues:
                    message = "\n".join(f"{n}  {m}" for n, m in value.issues.items())
                    self.host._log(message, feature=self.page_key, event="实时同步", level="warning")
                if value.changed:
                    counts = value.copies
                    self.host._log(f"已复制 {counts.copied_files} 个文件，创建 {value.created_dirs} 个目录，"
                                   f"改名 {counts.renamed_items} 项，删除 {counts.deleted_files} 个文件、"
                                   f"{counts.deleted_dirs} 个目录。", feature=self.page_key, event="实时同步")
                self.status = ""
        self.persist()
        self.render()
