"""Windows E: -> H: ordinary mirror, default read-only, one console confirmation.

No alternative state directory, preauthorization token, or retry option exists.
All engine safety checks remain in place. Reports are outside both repositories.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import ntpath
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from uuid import uuid4

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from obmanage import __version__
from obmanage.app import acquire_instance_lock
from obmanage.engine import SyncEngine
from obmanage.management.recovery import require_recovery_clear
from obmanage.models import SyncCancelled, SyncError, SyncPlan, SyncResult
from obmanage.paths import (assert_plain_chain, canonical, identity, native, revalidate_roots,
                            snapshot, validate_roots, validate_state_separation, hold_windows_directory)
from obmanage.scanning.metrics import ScanMetrics, phase
from obmanage.settings import default_state_dir
from tools.benchmark_readonly_paths import Directory
from tools.mirror_profile_report import NOTES, analysis, csv_report, hotspots, mechanism, summary

ROOT = Path(__file__).resolve().parents[1]
MAX_PREVIEW_AGE_NS = 600 * 1_000_000_000
MIN_MARGIN = 4 * 1024**3
DETAIL_PHASES = frozenset((
    "path_identity", "application_lock", "recovery_gate", "device_probe", "space_check",
    "source_scan", "target_scan", "planning", "source_sha256", "target_sha256",
    "final_revalidation", "analysis_final_source", "analysis_final_target", "execute_preflight",
    "preflight_source", "preflight_target", "target_mkdir", "copy_pipeline", "source_to_stage",
    "staged_sha256", "set_timestamps", "atomic_publish", "postwrite_recheck", "copy_baseline",
    "final_barrier", "barrier_source", "barrier_target", "delete_stage", "post_delete_revalidation",
    "sqlite_open", "sqlite_read", "sqlite_write", "sqlite_commit", "sqlite_finalize",
    "sqlite_register_temp", "sqlite_unregister_temp", "sqlite_close"))
STAGES = ("precheck", "analysis", "space_check", "confirmation", "confirmed_plan_revalidation", "target_parent_prepare",
          "execute", "post_quick_rescan", "post_deep_verify")
PLAN_FIELDS = ("add_files", "add_dirs", "update_files", "rename_items", "delete_files", "delete_dirs",
               "skip_files", "bytes_to_copy", "delete_bytes", "source_file_count", "target_file_count")
KNOWN_EXTENSIONS = frozenset((".md", ".json", ".js", ".css", ".html", ".py", ".txt", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".mkv", ".mov", ".avi", ".zip",
    ".mp3", ".wav", ".m4a", ".docx", ".xlsx", ".pptx", ".canvas", ".yaml", ".yml", ".ts"))


class Blocked(SyncError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=lambda x: sorted(x) if isinstance(x, set) else str(x)).encode("utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def repository():
    digest = hashlib.sha256()
    files = [*sorted((ROOT / "obmanage").rglob("*.py")), Path(__file__),
             ROOT / "tools/mirror_profile_report.py", ROOT / "requirements.txt"]
    for file in files:
        digest.update(file.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(file.read_bytes())
    return dict(git_commit=git("rev-parse", "HEAD"), git_dirty=bool(git("status", "--porcelain")),
                obmanage_version=__version__, implementation_sha256=digest.hexdigest())


def make_output(source, target):
    # Validate the entire writable workspace boundary before creating anything.
    roots = (str(source), str(target))
    workspace = Path(validate_state_separation(ROOT, roots))
    base = workspace / "artifacts" / "mirror-profile"
    if git("check-ignore", "artifacts/mirror-profile/probe") != "artifacts/mirror-profile/probe":
        raise Blocked("BLOCKED_REPORT_NOT_IGNORED")
    validate_state_separation(base, roots)
    with Directory.bind(workspace).hold():
        assert_plain_chain(base)
        base.mkdir(parents=True, exist_ok=True)
        binding = Directory.bind(base)
        with binding.hold():
            run_id = "mirror-" + datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
            work = base / run_id
            work.mkdir(mode=0o700)
    return run_id, work


class Report:
    def __init__(self, run_id, work, repo):
        self.work = work
        self.binding = Directory.bind(work)
        self.owned = {}
        self.origin_ns = time.perf_counter_ns()
        self.events = []
        self.private_log = []
        self.last_progress_ns = 0
        self.data = dict(schema_version="1.0", run_id=run_id, status="PENDING",
            started_at=now(), execution_performed=False, operator_confirmed=False,
            mode="mirror", direction="local_to_portable", repository=repo,
            environment=dict(os=platform.platform(), python=platform.python_version(),
                source_fs=None, target_fs=None, source_device_type=None, target_device_type=None,
                cache_state="unknown", target_original_exists=None, target_parent_exists=None),
            plan=dict.fromkeys(PLAN_FIELDS), timing_ms={}, scan_counters={},
            result=dict(engine_status="NOT_RUN", copied_files=None, copied_bytes=None,
                deleted_files=None, deleted_dirs=None, skipped_files=None, renamed_items=None, errors=[]),
            stages={name: dict(started_at=None, duration_ms=None, status="NOT_RUN") for name in STAGES},
            blockers=[], verification=dict(quick=dict(status="NOT_RUN"), deep=dict(status="NOT_RUN"),
                independent_audit=dict(status="NOT_RUN", reason="额外两端全量读盘成本，本工具未启用独立实现。"),
                source_metadata_stable=None),
            throughput=dict(effective_mirror_MiB_s=None, copy_phase_MiB_s=None,
                            source_read_MiB_s=None, staged_hash_MiB_s=None),
            measurement_notes=list(NOTES))

    def write(self, name, content):
        self.binding.verify()
        path = self.work / name
        if snapshot(path) is not None:
            if identity(snapshot(path)) != self.owned.get(name) or os.lstat(native(path)).st_nlink != 1:
                raise Blocked("BLOCKED_REPORT_REPLACED")
        elif name in self.owned:
            raise Blocked("BLOCKED_REPORT_REMOVED")
        stage = self.work / (".report-" + uuid4().hex)
        with open(native(stage), "x", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
        # This is an owned report file, never a repository object.
        os.replace(native(stage), native(path))
        self.owned[name] = identity(snapshot(path))

    def event(self, name, state, **values):
        # Reserve space for terminal stage events even during long full hashes.
        progress = name.startswith("progress_")
        if progress and len(self.events) >= 224:
            self.data["progress_events_dropped"] = self.data.get("progress_events_dropped", 0) + 1
            return
        if len(self.events) >= 256 and not progress:
            oldest = next((i for i, event in enumerate(self.events) if event["event"].startswith("progress_")), 0)
            self.events.pop(oldest)
        if len(self.events) < 256:
            self.events.append(dict(event=name, state=state, at=now(),
                offset_ms=round((time.perf_counter_ns() - self.origin_ns) / 1e6, 3), **values))
        else:
            self.data["progress_events_dropped"] = self.data.get("progress_events_dropped", 0) + 1

    def progress(self, event):
        stamp = time.perf_counter_ns()
        if stamp - self.last_progress_ns >= 5_000_000_000:
            self.last_progress_ns = stamp
            # Never copy event.message or relative_path into shared outputs.
            values = dict(files=event.completed_files, bytes=event.completed_bytes)
            self.event("progress_" + event.phase, "OBSERVED", **values)
            print(json.dumps(dict(phase=event.phase, **values)), flush=True)

    def measure(self, name, action):
        record = self.data["stages"][name]
        if record["status"] != "NOT_RUN":
            raise Blocked("BLOCKED_STAGE_RETRY")
        record.update(started_at=now(), status="RUNNING")
        self.event(name, "STARTED")
        self.save()
        metrics = ScanMetrics(name, detail_phases=DETAIL_PHASES)
        started = time.perf_counter_ns()
        try:
            with metrics.activate():
                result = action()
                if isinstance(result, SyncResult):
                    metrics.status = result.status
            record["status"] = ("FAILED" if isinstance(result, SyncPlan) and not result.can_execute
                                else result.status.upper() if isinstance(result, SyncResult) else "SUCCESS")
            return result
        except BaseException as exc:
            record["status"] = ("BLOCKED" if isinstance(exc, Blocked) else "CANCELLED"
                                if isinstance(exc, (SyncCancelled, KeyboardInterrupt)) else "FAILED")
            raise
        finally:
            record["duration_ms"] = round((time.perf_counter_ns() - started) / 1e6, 3)
            measured = metrics.report()
            for key in ("directory_enumerations", "directory_entries", "metadata_queries", "hash_files",
                        "hash_bytes", "baseline_hits", "baseline_stale_records", "source_read_bytes",
                        "stage_write_bytes", "sqlite_commits"):
                measured["counts"].setdefault(key, 0)
            record["metrics"] = dict(status=measured["status"], counts=measured["counts"],
                timing_ms={k: round(v * 1000, 3) for k, v in measured["seconds"].items()},
                scopes=measured.get("scopes", {}), spans={k: {
                    (field.replace("_ns", "_ms") if field.endswith("_ns") else field):
                    (round(value / 1e6, 3) if field.endswith("_ns") else value)
                    for field, value in span.items()} for k, span in measured.get("spans", {}).items()},
                overlap_group=name, origin_offset_ms=round((metrics._origin_ns - self.origin_ns) / 1e6, 3))
            self.event(name, record["status"], duration_ms=record["duration_ms"])
            self.save()

    def save(self):
        stages = self.data["stages"]
        self.data["timing_ms"] = {name: stage["duration_ms"] for name, stage in stages.items()}
        self.data["timing_ms"].update(analysis_total=stages["analysis"]["duration_ms"],
                                     execute_total=stages["execute"]["duration_ms"])
        mapping = dict(source_scan="source_scan", target_scan="target_scan", planning_inclusive="planning",
                       analysis_final_revalidation="final_revalidation", execute_preflight="execute_preflight",
                       source_to_stage_accumulated="source_to_stage", staged_sha256_accumulated="staged_sha256",
                       final_barrier="final_barrier", delete_stage="delete_stage")
        for alias, key in mapping.items():
            stage = stages["analysis" if key in ("source_scan", "target_scan", "planning", "final_revalidation") else "execute"]
            self.data["timing_ms"][alias] = stage.get("metrics", {}).get("timing_ms", {}).get(key)
        self.data["scan_counters"] = stages["analysis"].get("metrics", {}).get("counts", {})
        self.write("run_metrics.json", json.dumps(self.data, ensure_ascii=False, indent=2, allow_nan=False))
        self.write("timeline.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.events))
        self.write("hotspots.csv", csv_report(hotspots(self.data)))
        self.write("scan_mechanism.md", mechanism())
        self.write("chatgpt_summary.md", summary(self.data))
        self.write("analysis_report.md", analysis(self.data))
        self.write("run_local.log", "\n".join(self.private_log) + "\n")


def require_direction(source, target):
    if os.name != "nt":
        raise Blocked("BLOCKED_NO_WINDOWS_HOST")
    if (ntpath.splitdrive(str(source))[0].upper() != "E:"
            or ntpath.splitdrive(str(target))[0].upper() != "H:"):
        raise Blocked("BLOCKED_DIRECTION")
    for path in (source, target):
        if (not path.is_absolute() or ".." in path.parts or path == path.parent
                or any(os.path.isreserved(part) for part in path.parts[1:])):
            raise Blocked("BLOCKED_ABSOLUTE_PATH")


def probe_devices():
    command = """$ErrorActionPreference='Stop'
@('E','H') | ForEach-Object {
  $letter = $_
  $volume = Get-Volume -DriveLetter $letter
  $disk = Get-Partition -DriveLetter $letter | Get-Disk
  [pscustomobject]@{role=$letter;fs=[string]$volume.FileSystemType;bus=[string]$disk.BusType;
    disk=[int]$disk.Number;read_only=[bool]$disk.IsReadOnly;offline=[bool]$disk.IsOffline}
} | ConvertTo-Json -Compress
"""
    raw = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                  encoding="utf-8", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    rows = json.loads(raw)
    if not isinstance(rows, list) or len(rows) != 2:
        raise Blocked("BLOCKED_DEVICE_IDENTITY")
    return {row["role"]: row for row in rows}


def console_available():
    if os.name != "nt" or not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    mode = wintypes.DWORD()
    return bool(kernel.GetConsoleMode(kernel.GetStdHandle(-10), ctypes.byref(mode)))


def read_console_phrase(prompt, deadline_ns, cancel):
    """Bound the console wait too, so an abandoned preview releases its lock."""
    if not console_available():
        raise Blocked("BLOCKED_LOCAL_CONSOLE_REQUIRED")
    import msvcrt
    print(prompt, end="", flush=True)
    response = []
    while time.perf_counter_ns() <= deadline_ns:
        if cancel.is_set():
            raise SyncCancelled("现场确认已取消")
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        char = msvcrt.getwch()
        if char in ("\x03", "\x1a", "\x1b"):
            raise SyncCancelled("现场确认已取消")
        if char in ("\r", "\n"):
            print(flush=True)
            return "".join(response).strip()
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()
        elif char == "\b":
            if response:
                response.pop()
                print("\b \b", end="", flush=True)
        elif char.isprintable() and len(response) < 128:
            response.append(char)
            print(char, end="", flush=True)
    raise Blocked("BLOCKED_PREVIEW_EXPIRED_OR_CHANGED")


def require_target_access(path):
    """Request directory mutation rights without creating a probe file."""
    from ctypes import wintypes
    before = identity(assert_plain_chain(path))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # ADD_FILE | ADD_SUBDIRECTORY | DELETE_CHILD | READ_ATTRIBUTES; OPEN_EXISTING.
    handle = kernel.CreateFileW(native(path), 0xC6, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise Blocked("BLOCKED_TARGET_ACCESS")
    try:
        if identity(assert_plain_chain(path)) != before:
            raise Blocked("BLOCKED_TARGET_ACCESS_IDENTITY")
    finally:
        kernel.CloseHandle(handle)


def is_volume_root(path):
    path = Path(path)
    return path.is_absolute() and path == Path(path.anchor)


def volume_bound_child(anchor, child, volume):
    """Address a root's one child by stable GUID, never by a mutable drive letter.

    Some exFAT roots report inode zero. Do not accept that as an ordinary
    directory identity: create the new, nonzero-ID parent exclusively through
    the already verified volume namespace and retain its non-deletable handle.
    """
    anchor, child = Path(anchor), Path(child)
    guid = volume.rsplit(":", 2)[0]
    if (not is_volume_root(anchor) or child.parent != anchor or not child.name
            or os.path.isreserved(child.name)
            or not re.fullmatch(r"\\\\\?\\volume\{[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\}\\", guid, re.IGNORECASE)):
        raise Blocked("BLOCKED_PARENT_VOLUME_NAMESPACE")
    return guid + child.name


def verify_guid_handle(kernel, handle, path, volume):
    """Verify namespace, ordinary-directory type and volume using the open handle."""
    from ctypes import wintypes as w

    class BasicInfo(ctypes.Structure):
        _fields_ = (("created", ctypes.c_longlong), ("accessed", ctypes.c_longlong),
                    ("written", ctypes.c_longlong), ("changed", ctypes.c_longlong),
                    ("attributes", w.DWORD))

    kernel.GetFileInformationByHandleEx.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD)
    attrs = BasicInfo()
    # FILE_BASIC_INFO provides the same directory/reparse attribute bits and
    # also works for exFAT roots that reject FILE_ATTRIBUTE_TAG_INFO (error 87).
    if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(attrs), ctypes.sizeof(attrs)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not attrs.attributes & 0x10 or attrs.attributes & 0x400:
        raise Blocked("BLOCKED_PARENT_HANDLE_TYPE")
    kernel.GetFinalPathNameByHandleW.argtypes = (w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD)
    final = ctypes.create_unicode_buffer(32768)
    length = kernel.GetFinalPathNameByHandleW(handle, final, len(final), 1)  # VOLUME_NAME_GUID
    if not 0 < length < len(final) or os.path.normcase(final.value.rstrip("\\")) != os.path.normcase(path.rstrip("\\")):
        raise Blocked("BLOCKED_PARENT_HANDLE_NAMESPACE")
    kernel.GetVolumeInformationByHandleW.argtypes = (
        w.HANDLE, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD),
        ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD)
    serial, maximum, flags = w.DWORD(), w.DWORD(), w.DWORD()
    filesystem = ctypes.create_unicode_buffer(128)
    if not kernel.GetVolumeInformationByHandleW(handle, None, 0, ctypes.byref(serial),
            ctypes.byref(maximum), ctypes.byref(flags), filesystem, len(filesystem)):
        raise ctypes.WinError(ctypes.get_last_error())
    guid, expected_serial, expected_fs = volume.rsplit(":", 2)
    if (serial.value != int(expected_serial, 16) or filesystem.value.casefold() != expected_fs.casefold()
            or not os.path.normcase(path).startswith(os.path.normcase(guid))):
        raise Blocked("BLOCKED_PARENT_HANDLE_VOLUME")


@contextmanager
def verified_guid_directory(path, volume):
    """Open a read-only parent handle; a volume root may legitimately have ID zero."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    handle = kernel.CreateFileW(path, 0x100081, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        verify_guid_handle(kernel, handle, path, volume)
        if os.path.normcase(path.rstrip("\\")) != os.path.normcase(volume.rsplit(":", 2)[0].rstrip("\\")):
            state = identity(snapshot(path))
            if not state or not state[2]:
                raise Blocked("BLOCKED_PARENT_DIRECTORY_ID")
        yield handle
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def hold_guid_directory(path, volume, created=None):
    """Exclusive one-component NT creation relative to a verified parent handle.

    OBJ_DONT_REPARSE is retained on the relative child. Applying it to an
    absolute GUID path would reject the mount-manager namespace itself.
    """
    from ctypes import wintypes as w

    class UnicodeString(ctypes.Structure):
        _fields_ = (("length", w.USHORT), ("capacity", w.USHORT), ("text", w.LPWSTR))

    class ObjectAttributes(ctypes.Structure):
        _fields_ = (("length", w.ULONG), ("root", w.HANDLE),
                    ("name", ctypes.POINTER(UnicodeString)), ("flags", w.ULONG),
                    ("security", w.LPVOID), ("quality", w.LPVOID))

    parent, name = ntpath.split(path)
    if not name or os.path.isreserved(name) or name in (".", ".."):
        raise Blocked("BLOCKED_PARENT_VOLUME_NAMESPACE")
    # ntpath.split keeps the trailing slash on a GUID volume root.
    with verified_guid_directory(parent, volume) as parent_handle:
        dll = ctypes.WinDLL("ntdll")
        create = dll.NtCreateFile
        create.argtypes = (ctypes.POINTER(w.HANDLE), w.ULONG, ctypes.POINTER(ObjectAttributes),
            w.LPVOID, w.LPVOID, w.ULONG, w.ULONG, w.ULONG, w.ULONG, w.LPVOID, w.ULONG)
        create.restype = w.LONG
        dll.RtlNtStatusToDosError.argtypes = (w.LONG,)
        dll.RtlNtStatusToDosError.restype = w.ULONG
        length = len(name.encode("utf-16-le"))
        buffer = ctypes.create_unicode_buffer(name)
        text = UnicodeString(length, length + 2, ctypes.cast(buffer, w.LPWSTR))
        attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent_handle,
            ctypes.pointer(text), 0x1040, None, None)
        status_block = (ctypes.c_size_t * 2)()
        child = w.HANDLE()
        status = create(ctypes.byref(child), 0x100081, ctypes.byref(attributes), status_block,
                        None, 0, 3, 2, 0x21, None, 0)  # FILE_CREATE; DIRECTORY; no delete sharing
        if status < 0:
            raise ctypes.WinError(dll.RtlNtStatusToDosError(status))
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = (w.HANDLE,)
        try:
            if created:
                created()
            verify_guid_handle(kernel, child, path, volume)
            state = identity(snapshot(path))
            if not state or state[0] != "dir" or not state[2]:
                raise Blocked("BLOCKED_CREATED_PARENT_ID")
            yield state
        finally:
            kernel.CloseHandle(child)


@dataclass
class Preview:
    plan: SyncPlan
    digest: str
    created_ns: int
    preview_id: str

    @property
    def phrase(self):
        return "MIRROR_TO_H_" + self.digest[:8].upper()

    @property
    def delete_phrase(self):
        c = self.plan.counts
        return f"DELETE_{c['delete']}_FILES_{c['rmdir']}_DIRS_{self.digest[:8].upper()}"


class MirrorOnce:
    def __init__(self, source, target, state, report, cancel, *, execute=False, verify_deep=False,
                 create_target_parent=False):
        self.source, self.target, self.state = source, target, state
        self.report, self.cancel = report, cancel
        self.execute_requested, self.verify_deep = execute, verify_deep
        self.engine = SyncEngine(state)
        self.context = None
        self.lock = None
        self.execute_called = False
        self.create_target_parent = create_target_parent
        self.parent_intent = None
        self.created_parent_identity = None

    def check_cancel(self):
        if self.cancel.is_set():
            raise SyncCancelled("取消诊断")

    def precheck(self, resources):
        self.check_cancel()
        with phase("path_identity"):
            require_direction(self.source, self.target)
            self.context = validate_roots(str(self.source), str(self.target))
            # Do not quietly adopt 8.3 or drive aliases as the requested endpoints.
            if any(os.path.normcase(canonical(path)) != os.path.normcase(self.context[side])
                   for side, path in (("source", self.source), ("target", self.target))):
                raise Blocked("BLOCKED_PATH_ALIAS")
            if (self.context["source_volume"] == self.context["target_volume"]
                    or not all(self.context[side + "_volume"].startswith("\\\\?\\volume{")
                               for side in ("source", "target"))):
                raise Blocked("BLOCKED_VOLUME_IDENTITY")
            for path in (self.state, self.report.work, ROOT):
                validate_state_separation(path, (str(self.source), str(self.target)))
            self.state.mkdir(parents=True, exist_ok=True)
            resources.enter_context(Directory.bind(self.state).hold())
        with phase("application_lock"):
            self.lock = acquire_instance_lock(self.state)
            if self.lock is None:
                raise Blocked("BLOCKED_INSTANCE_BUSY_NORMAL_EXIT_REQUIRED")
            resources.callback(self.lock.unlock)
        with phase("recovery_gate"):
            require_recovery_clear(self.state)
        with phase("device_probe"):
            devices = probe_devices()
            if (devices["E"]["disk"] == devices["H"]["disk"] or devices["H"]["read_only"]
                    or any(row["offline"] for row in devices.values())):
                raise Blocked("BLOCKED_DEVICE_IDENTITY_OR_ACCESS")
            env = self.report.data["environment"]
            for side, letter in (("source", "E"), ("target", "H")):
                volume = self.context[side + "_volume"]
                if devices[letter]["fs"].casefold() != volume.rsplit(":", 1)[-1].casefold():
                    raise Blocked("BLOCKED_FILESYSTEM_IDENTITY")
                env[side + "_fs"] = devices[letter]["fs"]
                # A stable one-way identifier, never a raw GUID/serial/path.
                env[side + "_volume_id"] = hashlib.sha256(volume.encode()).hexdigest()[:16]
                bus = devices[letter]["bus"]
                env[side + "_device_type"] = bus if bus in {"USB", "NVMe", "SATA", "SAS", "SD", "MMC"} else None
            env["target_original_exists"] = self.context["target_root"] is not None
            env["target_parent_exists"] = snapshot(self.target.parent) is not None
            env["target_access_probe"] = "NOT_RUN"  # No write-probe file in live repositories.
            if self.create_target_parent and not env["target_parent_exists"]:
                anchor = self.target.parent.parent
                state = assert_plain_chain(anchor)
                if state is None or state["kind"] != "dir" or str(anchor) != self.context["target_anchor"]:
                    raise Blocked("BLOCKED_MULTIPLE_MISSING_PARENTS")
                self.parent_intent = dict(path=str(self.target.parent), anchor=str(anchor),
                                          anchor_identity=identity(state))
                self.report.data["additional_parent_creation_planned"] = True
        self.check()

    def check(self):
        self.check_cancel()
        if self.lock is None:
            raise Blocked("BLOCKED_INSTANCE_LOCK_MISSING")
        pid, _, _ = self.lock.getLockInfo()
        if not self.lock.isLocked() or pid != os.getpid():
            raise Blocked("BLOCKED_INSTANCE_LOCK_LOST")
        revalidate_roots(self.context)
        require_recovery_clear(self.state)
        validate_state_separation(self.state, (str(self.source), str(self.target)))

    def digest(self, plan, preview_id):
        return hashlib.sha256(json_bytes(dict(preview_id=preview_id, mode=plan.mode,
            source=plan.source, target=plan.target, pair=plan.pair_id, context=plan.context,
            additional_parent_creation=self.parent_intent,
            items=[asdict(item) for item in plan.items], counts=plan.counts,
            bytes_to_copy=plan.bytes_to_copy))).hexdigest()

    def space(self, plan):
        free = shutil.disk_usage(native(plan.context["target_anchor"])).free
        largest = max((item.size for item in plan.items if item.action in ("add", "update")), default=0)
        margin = max(MIN_MARGIN, plan.bytes_to_copy // 10)
        required = plan.bytes_to_copy + largest + margin
        self.report.data["space"] = dict(free_bytes=free, bytes_to_copy=plan.bytes_to_copy,
            largest_stage_bytes=largest, margin_bytes=margin, required_bytes=required,
            sufficient=free >= required)
        if plan.bytes_to_copy and free < required:
            raise Blocked("BLOCKED_INSUFFICIENT_SPACE")

    def preview(self):
        plan = self.report.measure("analysis", lambda: self.engine.analyze(str(self.source), str(self.target),
            deep=False, mode="mirror", cancel=self.cancel, progress=self.report.progress))
        self.report.private_log.extend(plan.errors)
        c = plan.counts
        self.report.data["plan"].update(dict(zip(PLAN_FIELDS[:7],
            (c["add"], c["mkdir"], c["update"], c["rename"], c["delete"], c["rmdir"], c["skip"]))))
        self.report.data["plan"].update(bytes_to_copy=plan.bytes_to_copy,
            delete_bytes=sum(item.size for item in plan.items if item.action == "delete"),
            error_items=c["error"], source_empty=plan.source_empty)
        for side in ("source", "target"):
            entries = plan.context.get(side + "_entries")
            if entries is not None:
                stats = inventory_stats(entries)
                self.report.data["plan"][side + "_file_count"] = stats["files"]
                self.report.data[side + "_inventory"] = stats
        preview_id = uuid4().hex
        preview = Preview(plan, self.digest(plan, preview_id), time.perf_counter_ns(), preview_id)
        self.report.data["preview"] = dict(id=preview_id, digest=preview.digest,
            maximum_age_ms=MAX_PREVIEW_AGE_NS / 1e6, mode="mirror", direction="local_to_portable")
        rows = [dict(**asdict(item), expected_target=str(self.target / item.relative_path)) for item in plan.items]
        self.report.write("private_plan.json", json.dumps(dict(source=plan.source, target=plan.target,
            counts=c, bytes_to_copy=plan.bytes_to_copy, preview_id=preview_id, digest=preview.digest,
            binding=self.context, additional_parent_creation=self.parent_intent, items=rows, errors=plan.errors), ensure_ascii=False, indent=2))
        deleted = [row for row in rows if row["action"] in ("delete", "rmdir")]
        # JSON-quoted paths prevent control characters from forging terminal lines.
        self.report.write("private_plan.md", "# 本机私有完整删除清单（禁止上传）\n\n" +
            "\n".join(json.dumps(row, ensure_ascii=False) for row in deleted) +
            ("\n无删除项。\n" if not deleted else "\n"))
        print(f"来源（只读）: {self.source}\n唯一写入目标: {self.target}\n预览: {preview_id}", flush=True)
        print(json.dumps(self.report.data["plan"], ensure_ascii=False), flush=True)
        print(f"全部逐项计划: {self.report.work / 'private_plan.json'}\n"
              f"全部目标删除项: {self.report.work / 'private_plan.md'}", flush=True)
        print("前 10 个非跳过项目：", flush=True)
        for row in [r for r in rows if r["action"] != "skip"][:10]:
            print(json.dumps(row, ensure_ascii=False), flush=True)
        print(f"目标删除：{c['delete']} 个文件、{c['rmdir']} 个目录，"
              f"{self.report.data['plan']['delete_bytes']:,} 字节，不可恢复。", flush=True)
        if not plan.can_execute or not plan.context.get("validated"):
            raise Blocked("BLOCKED_INCOMPLETE_PREVIEW")
        self.check()
        for side in ("source", "target"):
            expected = self.context[side + "_root"]
            if (identity(plan.context[side + "_root"]) != identity(expected)
                    or plan.context[side + "_volume"] != self.context[side + "_volume"]):
                raise Blocked("BLOCKED_ENDPOINT_CHANGED")
        for relative in plan.context["source_entries"].keys() | plan.context["target_entries"].keys():
            if (any(os.path.isreserved(part) or len(part.encode("utf-16-le")) // 2 > 255
                    for part in relative.split("/"))
                    or len(native(self.target / relative).encode("utf-16-le")) // 2 > 32700):
                raise Blocked("BLOCKED_UNSUPPORTED_PATH")
        if plan.source_empty and plan.context["target_entries"]:
            raise Blocked("BLOCKED_EMPTY_SOURCE")
        self.report.measure("space_check", lambda: self.space(plan))
        return preview

    def write_gate(self, preview):
        self.check()
        if self.execute_called:
            raise Blocked("BLOCKED_EXECUTE_ALREADY_CALLED")
        if (time.perf_counter_ns() - preview.created_ns > MAX_PREVIEW_AGE_NS
                or self.digest(preview.plan, preview.preview_id) != preview.digest):
            raise Blocked("BLOCKED_PREVIEW_EXPIRED_OR_CHANGED")
        # os.makedirs would otherwise create ancestors outside the authorized tree.
        parent = assert_plain_chain(self.target.parent)
        if self.parent_intent is not None:
            if self.created_parent_identity is None:
                if parent is not None:
                    raise Blocked("BLOCKED_PARENT_APPEARED_AFTER_PREVIEW")
                anchor = self.parent_intent["anchor"]
                if identity(assert_plain_chain(anchor)) != self.parent_intent["anchor_identity"]:
                    raise Blocked("BLOCKED_PARENT_ANCHOR_CHANGED")
                require_target_access(anchor)
                return
            if identity(parent) != self.created_parent_identity:
                raise Blocked("BLOCKED_CREATED_PARENT_REPLACED")
        if parent is None or parent["kind"] != "dir":
            raise Blocked("BLOCKED_TARGET_PARENT_MISSING")
        require_target_access(self.target if preview.plan.context["target_root"] is not None else self.target.parent)
        self.report.data["environment"]["target_access_probe"] = "DIRECTORY_RIGHTS_HANDLE_ONLY"

    def confirm(self, preview):
        self.write_gate(preview)
        if not console_available():
            raise Blocked("BLOCKED_LOCAL_CONSOLE_REQUIRED")
        print(f"绑定预览 {preview.preview_id}，摘要 {preview.digest}；mirror / E: → H:。\n"
              "请检查全部计划和删除清单，并暂停 Obsidian、同步客户端及其他外部写入。\n"
              "输入短语同时确认上述外部写入已暂停；其他输入取消。", flush=True)
        if self.parent_intent:
            print(f"补充授权范围：在 {self.parent_intent['anchor']} 下只创建空父目录 "
                  f"{self.parent_intent['path']}，再创建唯一目标树；此意图已绑定上述摘要。", flush=True)
        deadline = preview.created_ns + MAX_PREVIEW_AGE_NS
        if read_console_phrase(f"输入 {preview.phrase}: ", deadline, self.cancel) != preview.phrase:
            raise SyncCancelled("现场确认未匹配")
        c = preview.plan.counts
        if c["delete"] or c["rmdir"]:
            if read_console_phrase(f"永久删除 {c['delete']} 文件、{c['rmdir']} 目录，输入 {preview.delete_phrase}: ", deadline, self.cancel) != preview.delete_phrase:
                raise SyncCancelled("删除未确认")
        self.write_gate(preview)
        self.report.data["operator_confirmed"] = True
        self.report.data["external_writers_pause_acknowledged"] = True

    def prepare_parent(self, preview, resources):
        self.write_gate(preview)
        if not self.report.data["operator_confirmed"] or self.parent_intent is None:
            raise Blocked("BLOCKED_PARENT_CREATION_UNCONFIRMED")
        anchor = self.parent_intent["anchor"]
        if is_volume_root(anchor):
            bound_path = volume_bound_child(anchor, self.target.parent, self.context["target_volume"])
            self.write_gate(preview)
            # FILE_CREATE through the verified GUID cannot be redirected by a
            # drive-letter reassignment. The helper still requires a nonzero ID
            # on the newly created directory, and holds it without delete share.
            self.created_parent_identity = resources.enter_context(hold_guid_directory(
                bound_path, self.context["target_volume"],
                lambda: self.report.data.update(target_parent_created=True)))
            self.report.data["parent_creation_method"] = "VOLUME_GUID_EXCLUSIVE_HANDLE"
        else:
            resources.enter_context(hold_windows_directory(anchor, self.parent_intent["anchor_identity"]))
            self.write_gate(preview)
            # Exactly one exclusive mkdir, never recursive and never remove on failure.
            os.mkdir(native(self.target.parent))
            self.created_parent_identity = identity(assert_plain_chain(self.target.parent))
            self.report.data["parent_creation_method"] = "PARENT_DIRECTORY_HANDLE"
        self.report.data["target_parent_created"] = True
        self.write_gate(preview)

    def run(self, resources):
        self.report.measure("precheck", lambda: self.precheck(resources))
        preview = self.preview()
        if not preview.plan.has_changes:
            self.report.data["status"] = "NO_CHANGES"
            return
        self.write_gate(preview)
        if not self.execute_requested:
            raise Blocked("BLOCKED_DRY_RUN_CONFIRMATION_REQUIRED")
        self.report.measure("confirmation", lambda: self.confirm(preview))

        def recheck():
            self.write_gate(preview)
            self.space(preview.plan)
            self.engine.validate_plan(preview.plan, cancel=self.cancel, progress=self.report.progress)
        self.report.measure("confirmed_plan_revalidation", recheck)
        self.write_gate(preview)
        if self.parent_intent is not None:
            self.report.measure("target_parent_prepare", lambda: self.prepare_parent(preview, resources))
        # Pin the verified parent against replacement; never pin/create unrelated trees.
        resources.enter_context(Directory.bind(self.target.parent).hold())
        self.execute_called = True
        self.report.data["execution_performed"] = True
        result = self.report.measure("execute", lambda: self.engine.execute(preview.plan,
            allow_empty=False, cancel=self.cancel, progress=self.report.progress))
        self.report.private_log.extend(result.errors)
        self.report.data["result"].update({k: v for k, v in asdict(result).items() if k not in ("errors", "duration_seconds")})
        self.report.data["result"]["engine_duration_ms"] = round(result.duration_seconds * 1000, 3)
        self.report.data["result"]["engine_status"] = result.status.upper()
        self.report.data["result"]["errors"] = [dict(category="EngineReportedError", stage="execute") for _ in result.errors]
        self.report.data["stages"]["execute"]["status"] = result.status.upper()
        rates(self.report.data, result)
        self.report.data["status"] = result.status.upper()
        self.report.save()
        if self.cancel.is_set():
            return
        quick = self.rescan("post_quick_rescan", False, "quick", preview.plan)
        if result.status != "success":
            return
        deep_ok = False
        if self.verify_deep:
            deep_ok = self.rescan("post_deep_verify", True, "deep", preview.plan)
        self.report.data["status"] = ("FAILED" if not quick or (self.verify_deep and not deep_ok)
                                      else "SUCCESS_VERIFIED" if deep_ok else "SUCCESS_PARTIALLY_VERIFIED")

    def rescan(self, stage, deep, label, original):
        try:
            observed = self.report.measure(stage, lambda: self.engine.analyze(str(self.source), str(self.target),
                deep=deep, mode="mirror", cancel=self.cancel, progress=self.report.progress))
        except BaseException as exc:
            self.report.data["verification"][label]["status"] = ("CANCELLED" if isinstance(exc, (SyncCancelled, KeyboardInterrupt)) else "FAILED")
            raise
        stable = observed.context.get("source_entries") == original.context["source_entries"]
        passed = observed.can_execute and not observed.has_changes and stable
        self.report.data["verification"][label] = dict(status="PASSED" if passed else "FAILED",
                                                     counts=observed.counts, error_count=len(observed.errors))
        self.report.data["verification"]["source_metadata_stable"] = stable
        self.report.private_log.extend(observed.errors)
        return passed


def inventory_stats(entries):
    sizes, extensions = Counter(), Counter()
    files = total = max_depth = 0
    for name, state in entries.items():
        max_depth = max(max_depth, name.count("/") + 1)
        if state["kind"] != "file":
            continue
        files += 1
        size = state["size"]
        total += size
        bucket = next((label for bound, label in ((4096, "lt_4KiB"), (65536, "4_64KiB"),
                      (1024**2, "64KiB_1MiB"), (100 * 1024**2, "1_100MiB")) if size < bound), "ge_100MiB")
        sizes[bucket] += 1
        suffix = Path(name).suffix.lower()
        extensions[suffix if suffix in KNOWN_EXTENSIONS else "other"] += 1
    return dict(files=files, directories=len(entries) - files, bytes=total, max_depth=max_depth,
                size_buckets=dict(sizes), extension_groups=dict(extensions))


def rates(data, result):
    record = data["stages"]["execute"]
    metric = record["metrics"]
    timing, counts = metric["timing_ms"], metric["counts"]
    for label, size, ms in (
        ("effective_mirror_MiB_s", result.copied_bytes, record["duration_ms"]),
        ("copy_phase_MiB_s", result.copied_bytes if result.status == "success" else None, timing.get("copy_pipeline")),
        ("source_read_MiB_s", counts.get("source_read_bytes"), timing.get("source_read")),
        ("staged_hash_MiB_s", metric["scopes"].get("staged_sha256", {}).get("hash_bytes"), timing.get("staged_sha256"))):
        data["throughput"][label] = round(size / 1024**2 / (ms / 1000), 3) if size is not None and ms else None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="本机 E: 普通镜像来源（只读）")
    parser.add_argument("--target", required=True, type=Path, help="移动端 H: 唯一授权目标树")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="只读预览并生成报告（默认）")
    modes.add_argument("--execute", action="store_true", help="新预览 + 本地 console 现场确认，仅执行一次")
    parser.add_argument("--verify-deep", action="store_true", help="执行后另做引擎完整内容校验，单独计时")
    parser.add_argument("--create-target-parent", action="store_true",
                        help="须另获授权；将唯一缺失直接父目录的独占创建绑定本地确认，禁止递归创建")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cancel = Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    report = None
    try:
        run_id, work = make_output(args.source, args.target)
        with Directory.bind(work).hold(), ExitStack() as resources:
            report = Report(run_id, work, repository())
            report.private_log.append("Reproduce argv: " + json.dumps([sys.executable, str(Path(__file__)),
                                      *(argv if argv is not None else sys.argv[1:])], ensure_ascii=False))
            try:
                state = Path(validate_state_separation(default_state_dir(), (str(args.source), str(args.target))))
                runner = MirrorOnce(args.source, args.target, state, report, cancel,
                                    execute=args.execute, verify_deep=args.verify_deep,
                                    create_target_parent=args.create_target_parent)
                runner.run(resources)
            except BaseException as exc:
                report.private_log.append(traceback.format_exc())
                report.data["status"] = ("CANCELLED" if isinstance(exc, (SyncCancelled, KeyboardInterrupt, EOFError))
                    else "FAILED" if report.data["execution_performed"] else "BLOCKED")
                if report.data["result"]["engine_status"] == "SUCCESS":
                    report.data["status"] = "SUCCESS_PARTIALLY_VERIFIED"
                elif report.data["execution_performed"] and report.data["result"]["engine_status"] == "NOT_RUN":
                    report.data["result"]["engine_status"] = "UNKNOWN_PARTIAL"
                report.data["blockers"].append(exc.code if isinstance(exc, Blocked) else type(exc).__name__)
            finally:
                report.data["finished_at"] = now()
                for name, stage in report.data["stages"].items():
                    if stage["status"] == "NOT_RUN":
                        stage["reason"] = (report.data["blockers"][-1] if report.data["blockers"]
                                           else "NOT_SELECTED_OR_NOT_REACHED")
                report.save()
            print(f"状态: {report.data['status']}；真实 execute: {report.data['execution_performed']}", flush=True)
            print(f"报告目录: {work}", flush=True)
            print(summary(report.data), flush=True)
            return 0 if report.data["status"] in ("SUCCESS_VERIFIED", "SUCCESS_PARTIALLY_VERIFIED", "NO_CHANGES") else 2
    except Exception as exc:
        # Before safe output creation, do not emit raw exceptions with private paths.
        print(f"BLOCKED: {exc.code if isinstance(exc, Blocked) else type(exc).__name__}；未取得安全报告边界。", flush=True)
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
