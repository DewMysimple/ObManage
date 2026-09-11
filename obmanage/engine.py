"""Incremental, one-way mirror engine; deliberately independent of Qt.

The preview carries a snapshot that is revalidated before execution. Only fully
verified copies establish baselines, and deletion starts only after all copies
have succeeded and the complete source has been scanned again.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from .models import PlanItem, Progress, SyncCancelled, SyncError, SyncPlan, SyncResult
from .paths import (assert_plain_chain, canonical, checked_child, identity, native,
                    revalidate_roots, snapshot, validate_roots,
                    validate_state_separation)
from .store import BaselineStore

CHUNK_SIZE = 4 * 1024 * 1024
HASH_PROGRESS_INTERVAL = 0.08
ProgressCallback = Callable[[Progress], None] | None
_WINDOWS_ACCESS_DENIED = 5
_WINDOWS_ALREADY_EXISTS = 183
_WINDOWS_READONLY = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_DELETE_READONLY_ERRORS = frozenset((_WINDOWS_ACCESS_DENIED,))
_WINDOWS_REPLACE_READONLY_ERRORS = frozenset(
    (_WINDOWS_ACCESS_DENIED, _WINDOWS_ALREADY_EXISTS)
)
_RESERVED_DIRECTORY_PREFIXES = (".obmanage-deploy-",)
_ENGINE_TASK_LOCK = threading.Lock()


def _cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("已取消。已经完成的文件和校验记录会保留。")


def _emit(callback: ProgressCallback, phase: str, **kwargs) -> None:
    if callback is not None:
        callback(Progress(phase, **kwargs))


def _key(relative: str) -> str:
    return os.path.normcase(relative)


def _scan(root: str, cancel: threading.Event | None, progress: ProgressCallback) -> dict[str, dict]:
    _cancelled(cancel)
    root_state = snapshot(root)
    if root_state is None:
        return {}
    assert_plain_chain(root)
    entries: dict[str, dict] = {}
    seen: set[str] = set()
    stack = [(root, "")]
    while stack:
        _cancelled(cancel)
        folder, prefix = stack.pop()
        assert_plain_chain(folder)
        try:
            with os.scandir(native(folder)) as iterator:
                for entry in iterator:
                    _cancelled(cancel)
                    relative = prefix + entry.name
                    state = snapshot(entry.path)
                    if state is None:
                        raise SyncError(f"扫描时文件消失，请重新分析：{relative}")
                    if state["kind"] not in ("file", "dir"):
                        raise SyncError(f"不支持链接、重解析点或特殊文件：{relative}")
                    if state["kind"] == "dir" and any(
                        entry.name.casefold().startswith(prefix.casefold())
                        for prefix in _RESERVED_DIRECTORY_PREFIXES
                    ):
                        # Deployment staging/backup/rollback trees are recovery
                        # data owned by ObManage.  They are deliberately outside
                        # the mirror namespace so a later mirror cannot copy or
                        # delete the only recoverable original.
                        continue
                    if _key(relative) in seen:
                        raise SyncError(f"存在 Windows 无法区分的大小写重名路径：{relative}")
                    seen.add(_key(relative))
                    entries[relative] = state
                    if state["kind"] == "dir":
                        stack.append((canonical(entry.path), relative + "/"))
                    if len(entries) % 100 == 0:
                        _emit(progress, "scan", relative_path=relative,
                              message=f"已扫描 {len(entries):,} 个项目", completed_files=len(entries))
        except OSError as exc:
            raise SyncError(f"无法完整扫描目录 {folder}：{exc}") from exc
    _emit(progress, "scan", message=f"扫描完成：{len(entries):,} 个项目", completed_files=len(entries))
    return entries


def _require_state(path: str, expected: dict | None, message: str) -> None:
    actual = snapshot(path)
    if actual != expected:
        raise SyncError(f"{message}，请重新分析差异：{path}")


def _hash_file(path: str, expected: dict, cancel: threading.Event | None,
               progress: ProgressCallback, relative_path: str) -> str:
    """Hash only a stable version of the file, never silently adopt a new version."""
    _cancelled(cancel)
    _require_state(path, expected, "校验前文件已改变")
    digest = hashlib.sha256()
    read_bytes = 0
    with open(native(path), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns) != (
                expected["inode"], expected["device"], expected["size"], expected["mtime_ns"]):
            raise SyncError(f"打开文件时内容已改变：{relative_path}")
        while chunk := stream.read(CHUNK_SIZE):
            _cancelled(cancel)
            digest.update(chunk)
            read_bytes += len(chunk)
            _emit(progress, "hash", relative_path=relative_path,
                  message="正在校验内容", completed_bytes=read_bytes, total_bytes=expected["size"])
        closed = os.fstat(stream.fileno())
        if (closed.st_size, closed.st_mtime_ns, closed.st_ctime_ns) != (
                opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise SyncError(f"校验期间文件发生变化：{relative_path}")
    _cancelled(cancel)
    _require_state(path, expected, "校验期间文件已改变")
    if read_bytes != expected["size"]:
        raise SyncError(f"校验时读取的大小不符：{relative_path}")
    return digest.hexdigest()


class _CombinedCancel:
    """Expose one cancellation flag to hash workers without mutating the caller's event."""

    def __init__(self, external: threading.Event | None, internal: threading.Event):
        self.external = external
        self.internal = internal

    def is_set(self) -> bool:
        return self.internal.is_set() or (self.external is not None and self.external.is_set())


def _hash_pair(source_path: str, source_expected: dict,
               target_path: str, target_expected: dict,
               cancel: threading.Event | None, progress: ProgressCallback,
               relative_path: str, executor: ThreadPoolExecutor | None) -> tuple[str, str]:
    """Hash both stable versions, overlapping reads only when their volumes differ."""
    completed = [0, 0]
    total_bytes = source_expected["size"] + target_expected["size"]

    def emit_progress() -> None:
        if total_bytes:
            _emit(progress, "hash", relative_path=relative_path,
                  message="正在校验两端内容", completed_bytes=sum(completed),
                  total_bytes=total_bytes)

    if executor is None:
        def sequential_progress(index: int) -> Callable[[Progress], None]:
            def update(event: Progress) -> None:
                completed[index] = event.completed_bytes
                emit_progress()
            return update

        source_digest = _hash_file(
            source_path, source_expected, cancel, sequential_progress(0), relative_path
        )
        target_digest = _hash_file(
            target_path, target_expected, cancel, sequential_progress(1), relative_path
        )
    else:
        progress_lock = threading.Lock()
        abort = threading.Event()
        worker_cancel = _CombinedCancel(cancel, abort)

        def remember_progress(index: int) -> Callable[[Progress], None]:
            def update(event: Progress) -> None:
                with progress_lock:
                    completed[index] = event.completed_bytes
            return update

        futures: list[Future[str]] = []
        try:
            futures.append(executor.submit(
                _hash_file, source_path, source_expected, worker_cancel,
                remember_progress(0), relative_path
            ))
            futures.append(executor.submit(
                _hash_file, target_path, target_expected, worker_cancel,
                remember_progress(1), relative_path
            ))
        except BaseException:
            abort.set()
            wait(futures)
            raise

        reported = -1
        try:
            while True:
                done, _ = wait(futures, timeout=HASH_PROGRESS_INTERVAL,
                               return_when=FIRST_EXCEPTION)
                with progress_lock:
                    checked = sum(completed)
                if checked != reported:
                    reported = checked
                    if total_bytes:
                        _emit(progress, "hash", relative_path=relative_path,
                              message="正在并行校验两端内容", completed_bytes=checked,
                              total_bytes=total_bytes)
                errors = [future.exception() for future in done if future.exception() is not None]
                if errors:
                    abort.set()
                    wait(futures)
                    errors = [future.exception() for future in futures
                              if future.exception() is not None]
                    # Preserve the real read/state error when the peer stopped
                    # only because the pair was aborted.
                    error = next((value for value in errors
                                  if not isinstance(value, SyncCancelled)), errors[0])
                    raise error
                if len(done) == len(futures):
                    break
                if cancel is not None and cancel.is_set():
                    abort.set()
                    wait(futures)
                    raise SyncCancelled("已取消。已经完成的文件和校验记录会保留。")
            source_digest, target_digest = (future.result() for future in futures)
        except BaseException:
            abort.set()
            wait(futures)
            raise

    # One side can finish before the other. Recheck both paths only after the
    # pair has joined so a later change to the early side cannot be accepted.
    _cancelled(cancel)
    _require_state(source_path, source_expected, "比较期间源文件已改变")
    _require_state(target_path, target_expected, "比较期间目标文件已改变")
    return source_digest, target_digest


def _stat_matches_file_snapshot(value: os.stat_result, expected: dict) -> bool:
    return (stat.S_ISREG(value.st_mode)
            and not getattr(value, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
            and value.st_nlink == 1
            and (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns) == (
                expected["device"], expected["inode"], expected["size"],
                expected["mtime_ns"], expected["ctime_ns"]
            ))


def _restore_windows_readonly(path: str, writable: dict | None) -> None:
    """Best-effort rollback only while the path still has the cleared snapshot."""
    if writable is None:
        return
    try:
        if _stat_matches_file_snapshot(os.lstat(native(path)), writable):
            os.chmod(native(path), stat.S_IREAD)
    except OSError:
        pass


def _clear_windows_readonly(path: str, expected: dict | None, message: str) -> dict | None:
    """Clear only a verified target file's DOS read-only bit for one retry."""
    if os.name != "nt" or expected is None or expected.get("kind") != "file":
        return None
    current_stat = os.lstat(native(path))
    if not getattr(current_stat, "st_file_attributes", 0) & _WINDOWS_READONLY:
        return None
    _require_state(path, expected, message)
    verified_stat = os.lstat(native(path))
    if (not _stat_matches_file_snapshot(verified_stat, expected)
            or not getattr(verified_stat, "st_file_attributes", 0) & _WINDOWS_READONLY):
        raise SyncError(f"{message}，已停止处理：{path}")
    os.chmod(native(path), stat.S_IWRITE)
    try:
        writable = snapshot(path)
    except OSError:
        _restore_windows_readonly(path, expected)
        raise
    if (writable is None
            or identity(writable) != identity(expected)
            or writable["kind"] != "file"
            or (writable["size"], writable["mtime_ns"]) != (expected["size"], expected["mtime_ns"])):
        if writable is not None and identity(writable) == identity(expected):
            _restore_windows_readonly(path, writable)
        raise SyncError(f"{message}，已停止处理：{path}")
    try:
        writable_stat = os.lstat(native(path))
    except OSError:
        _restore_windows_readonly(path, writable)
        raise
    if not _stat_matches_file_snapshot(writable_stat, writable):
        current = snapshot(path)
        if current is not None and identity(current) == identity(expected):
            _restore_windows_readonly(path, current)
        raise SyncError(f"{message}，已停止处理：{path}")
    if getattr(writable_stat, "st_file_attributes", 0) & _WINDOWS_READONLY:
        _restore_windows_readonly(path, writable)
        raise SyncError(f"无法解除目标文件的只读属性：{path}")
    return writable


def _mutate_target_file(path: str, expected: dict | None, message: str,
                        operation: Callable[[], None], retry_winerrors: frozenset[int]) -> None:
    """Retry a Windows target mutation only when a verified file is read-only."""
    try:
        operation()
        return
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) not in retry_winerrors:
            raise
        writable = _clear_windows_readonly(path, expected, message)
        if writable is None:
            raise
    try:
        operation()
    except BaseException:
        _restore_windows_readonly(path, writable)
        raise


class SyncEngine:
    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(canonical(state_dir))
        # The desktop product promises one active analysis/sync task. Keep that
        # invariant even when callers accidentally construct multiple engines.
        self._lock = _ENGINE_TASK_LOCK

    def _check_state_location(self, source: str, target: str) -> None:
        self.state_dir = Path(
            validate_state_separation(self.state_dir, (source, target))
        )

    def analyze(self, source: str, target: str, *, deep: bool = False,
                cancel: threading.Event | None = None, progress: ProgressCallback = None) -> SyncPlan:
        if not self._lock.acquire(blocking=False):
            raise SyncError("已有同步任务运行中。")
        plan = SyncPlan(source=str(source), target=str(target))
        store: BaselineStore | None = None
        hash_pool: ThreadPoolExecutor | None = None
        try:
            _cancelled(cancel)
            context = validate_roots(str(source), str(target))
            self._check_state_location(context["source"], context["target"])
            plan.source, plan.target = context["source"], context["target"]
            plan.pair_id = context["pair_id"]
            plan.context = context
            source_entries = _scan(plan.source, cancel, progress)
            target_entries = _scan(plan.target, cancel, progress)
            context["source_entries"] = source_entries
            context["target_entries"] = target_entries
            context["target_names"] = {_key(path): path for path in target_entries}
            plan.source_empty = not source_entries
            store = BaselineStore(self.state_dir)
            baselines = store.records(plan.pair_id)
            # Equality of two verified file versions is symmetric. Keep each
            # direction's records separate, but reuse the opposite direction
            # only when *both* exact snapshots still match after swapping. The
            # reversed key includes the ordered canonical paths and volumes.
            reverse_baselines = store.records(context["reverse_pair_id"])
            source_by_key = {_key(path): state for path, state in source_entries.items()}
            target_by_key = {_key(path): state for path, state in target_entries.items()}
            stale_records = []
            for path, record in baselines.items():
                key = _key(path)
                if (key not in source_by_key or key not in target_by_key
                        or record[:2] != (source_by_key[key], target_by_key[key])):
                    stale_records.append((plan.pair_id, path))
            for path, record in reverse_baselines.items():
                key = _key(path)
                if (key not in source_by_key or key not in target_by_key
                        or record[:2] != (target_by_key[key], source_by_key[key])):
                    stale_records.append((context["reverse_pair_id"], path))
            invalidated_records = list(stale_records)
            if deep:
                # Retire old equality claims before reading content. A crash,
                # cancellation, or later database error therefore cannot make
                # a completed deep mismatch disappear into the old cache.
                invalidated_records.extend(
                    (plan.pair_id, path) for path in baselines
                )
                invalidated_records.extend(
                    (context["reverse_pair_id"], path) for path in reverse_baselines
                )
            if invalidated_records:
                # Once an observed file version no longer matches its equality
                # claim, never let that claim become valid again by coincidence.
                store.invalidate(invalidated_records)
            stale_keys = {
                (pair_id, _key(path)) for pair_id, path in invalidated_records
            }
            baseline_keys = {
                _key(path): (path, record) for path, record in baselines.items()
                if (plan.pair_id, _key(path)) not in stale_keys
            }
            reverse_keys = {
                _key(path): (path, record) for path, record in reverse_baselines.items()
                if (context["reverse_pair_id"], _key(path)) not in stale_keys
            }
            if context["source_volume"] != context["target_volume"]:
                # At most one sequential reader per volume: overlap C: and the
                # portable disk without making either disk seek across files.
                hash_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="obmanage-hash")
            source_names = {_key(path) for path in source_entries}
            total = len(source_entries) + sum(_key(path) not in source_names for path in target_entries)
            for relative, src in sorted(source_entries.items()):
                _cancelled(cancel)
                target_name = context["target_names"].get(_key(relative), relative)
                dst = target_entries.get(target_name)
                if (dst is not None and src["kind"] == dst["kind"] and os.name == "nt"
                        and relative.rsplit("/", 1)[-1] != target_name.rsplit("/", 1)[-1]):
                    plan.items.append(PlanItem("rename", relative, reason="统一文件名大小写，无需复制内容"))
                if dst is not None and src["kind"] != dst["kind"]:
                    item = PlanItem("error", relative, reason="源与目标存在文件/文件夹同名冲突")
                    plan.errors.append(f"{relative}：{item.reason}")
                elif src["kind"] == "dir":
                    # Existing directories require no action and are not counted
                    # among skipped files.
                    if dst is not None:
                        continue
                    item = PlanItem("mkdir", relative, reason="创建空目录或父目录")
                elif dst is None:
                    item = PlanItem("add", relative, src["size"], "目标中不存在")
                elif (not deep and _key(relative) in baseline_keys
                      and baseline_keys[_key(relative)][1][:2] == (src, dst)):
                    item = PlanItem("skip", relative, src["size"], "两端与上次校验记录一致")
                elif (not deep and _key(target_name) in reverse_keys
                      and reverse_keys[_key(target_name)][1][:2] == (dst, src)):
                    reverse_record = reverse_keys[_key(target_name)][1]
                    store.save(plan.pair_id, relative, src, dst, reverse_record[2])
                    baseline_keys[_key(relative)] = (
                        relative, (src, dst, reverse_record[2])
                    )
                    item = PlanItem("skip", relative, src["size"], "两端与反向同步的已校验记录一致")
                elif src["size"] != dst["size"]:
                    item = PlanItem("update", relative, src["size"], "文件大小不同，以源端为准")
                else:
                    src_path = checked_child(plan.source, relative)
                    dst_path = checked_child(plan.target, target_name)
                    src_digest, dst_digest = _hash_pair(
                        src_path, src, dst_path, dst, cancel, progress, relative, hash_pool
                    )
                    if src_digest == dst_digest:
                        verified = (src, dst, src_digest)
                        store.save(plan.pair_id, relative, src, dst, src_digest)
                        baseline_keys[_key(relative)] = (relative, verified)
                        item = PlanItem("skip", relative, src["size"], "SHA-256 内容相同")
                    else:
                        # A confirmed mismatch supersedes every prior equality
                        # claim in both directions. Invalidate atomically so a
                        # later quick scan cannot resurrect a known bad copy.
                        store.invalidate([
                            (plan.pair_id, relative),
                            (context["reverse_pair_id"], target_name),
                        ])
                        baseline_keys.pop(_key(relative), None)
                        reverse_keys.pop(_key(target_name), None)
                        item = PlanItem("update", relative, src["size"], "内容不同，以源端为准")
                plan.items.append(item)
                _emit(progress, "compare", relative_path=relative,
                      message=item.reason, completed_files=len(plan.items), total_files=total)
            for relative, dst in sorted(target_entries.items()):
                _cancelled(cancel)
                if _key(relative) not in source_names:
                    plan.items.append(PlanItem("rmdir" if dst["kind"] == "dir" else "delete",
                                               relative, dst["size"], "源端已不存在"))
                    _emit(progress, "compare", relative_path=relative,
                          message="目标多余项目", completed_files=len(plan.items), total_files=total)
            _cancelled(cancel)
            revalidate_roots(context)
            # A manifest scan is metadata-only, including for large unchanged
            # videos. It makes a preview generated during a write fail closed.
            if _scan(plan.source, cancel, None) != source_entries or _scan(plan.target, cancel, None) != target_entries:
                raise SyncError("分析期间目录内容发生变化，请重新分析差异。")
            context["validated"] = True
            _emit(progress, "done", message="差异分析完成", completed_files=total, total_files=total)
        except SyncCancelled:
            raise
        except (OSError, SyncError, ValueError, sqlite3.Error) as exc:
            plan.errors.append(str(exc))
            plan.items.append(PlanItem("error", "", reason=str(exc)))
        finally:
            if hash_pool is not None:
                hash_pool.shutdown(wait=True, cancel_futures=True)
            if store is not None:
                store.close()
            self._lock.release()
        return plan

    def _target_expected(self, plan: SyncPlan, relative: str) -> dict | None:
        if hasattr(self, "_execution_target_states"):
            return self._execution_target_states.get(_key(relative))
        actual_name = plan.context["target_names"].get(_key(relative), relative)
        return plan.context["target_entries"].get(actual_name)

    def _copy_file(self, plan: SyncPlan, item: PlanItem, store: BaselineStore,
                   cancel: threading.Event | None, progress: ProgressCallback,
                   result: SyncResult, target_root: dict) -> dict:
        relative = item.relative_path
        expected = plan.context["source_entries"][relative]
        src_path = checked_child(plan.source, relative)
        dst_path = checked_child(plan.target, relative)
        _require_state(src_path, expected, "复制前源文件已改变")
        _require_state(dst_path, self._target_expected(plan, relative), "复制前目标文件已改变")
        revalidate_roots(plan.context, target_root=target_root)
        temp_path: str | None = None
        temp_identity: tuple | None = None
        temp_fd: int | None = None
        try:
            temp_fd, temp_path = tempfile.mkstemp(prefix=".obmanage-", suffix=".tmp",
                                                   dir=native(os.path.dirname(dst_path)))
            temp_identity = identity(snapshot(temp_path))
            store.register_temp(plan.pair_id, temp_path, temp_identity)
            copied = 0
            digest = hashlib.sha256()
            with os.fdopen(temp_fd, "wb") as output:
                temp_fd = None
                with open(native(src_path), "rb") as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns) != (
                            expected["inode"], expected["device"], expected["size"], expected["mtime_ns"]):
                        raise SyncError(f"打开源文件时文件已改变：{relative}")
                    while chunk := source.read(CHUNK_SIZE):
                        _cancelled(cancel)
                        output.write(chunk)
                        digest.update(chunk)
                        copied += len(chunk)
                        _emit(progress, "copy", relative_path=relative, message="正在复制",
                              completed_bytes=result.copied_bytes + copied, total_bytes=plan.bytes_to_copy,
                              completed_files=result.copied_files,
                              total_files=plan.counts["add"] + plan.counts["update"])
                    finished = os.fstat(source.fileno())
                    if (finished.st_size, finished.st_mtime_ns, finished.st_ctime_ns) != (
                            opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                        raise SyncError(f"复制期间源文件发生变化：{relative}")
                output.flush()
                os.fsync(output.fileno())
            _cancelled(cancel)
            _require_state(src_path, expected, "复制期间源文件已改变")
            if copied != expected["size"]:
                raise SyncError(f"复制的文件大小不符：{relative}")
            temp_state = snapshot(temp_path)
            if _hash_file(temp_path, temp_state, cancel, progress, relative) != digest.hexdigest():
                raise SyncError(f"写入后内容校验失败：{relative}")
            # Copy only timestamps; carrying a read-only mode onto a staging
            # file would complicate cancellation cleanup and future overwrites.
            source_stat = os.stat(native(src_path), follow_symlinks=False)
            os.utime(temp_path, ns=(source_stat.st_atime_ns, expected["mtime_ns"]))
            staged_state = snapshot(temp_path)
            _cancelled(cancel)
            revalidate_roots(plan.context, target_root=target_root)
            checked_child(plan.target, relative)
            _require_state(src_path, expected, "提交前源文件已改变")
            target_expected = self._target_expected(plan, relative)
            _require_state(dst_path, target_expected, "提交前目标文件已改变")
            _mutate_target_file(
                dst_path,
                target_expected,
                "提交前目标文件已改变",
                lambda: os.replace(temp_path, native(dst_path)),
                _WINDOWS_REPLACE_READONLY_ERRORS,
            )
            store.unregister_temp(temp_path)
            temp_path = None
            result.copied_files += 1
            result.copied_bytes += copied
            dst_state = snapshot(dst_path)
            if dst_state is None or dst_state["kind"] != "file" or dst_state["size"] != copied:
                raise SyncError(f"提交后目标文件发生变化：{relative}")
            # FAT-family filesystems can issue a new file ID when a directory
            # entry is renamed, so an inode mismatch after os.replace does not
            # by itself prove that another process replaced the file. Keep the
            # common-filesystem fast path, but verify the stable final path by
            # content before accepting any identity or timestamp change.
            if (identity(dst_state) != identity(staged_state)
                    or dst_state["mtime_ns"] != staged_state["mtime_ns"]):
                if _hash_file(dst_path, dst_state, cancel, progress, relative) != digest.hexdigest():
                    raise SyncError(f"提交后目标文件发生变化：{relative}")
            _require_state(src_path, expected, "提交后源文件已改变")
            store.save(plan.pair_id, relative, expected, dst_state, digest.hexdigest())
            _emit(progress, "copied", relative_path=relative, message="文件已复制并校验",
                  completed_bytes=result.copied_bytes, total_bytes=plan.bytes_to_copy,
                  completed_files=result.copied_files,
                  total_files=plan.counts["add"] + plan.counts["update"])
            return dst_state
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_path is not None:
                # Never glob temp names, and never unlink a replacement with a
                # different identity. A crash/failed cleanup remains visible in
                # the next preview as a target-only file.
                try:
                    assert_plain_chain(os.path.dirname(canonical(temp_path)))
                    if identity(snapshot(temp_path)) == temp_identity:
                        os.unlink(temp_path)
                    if snapshot(temp_path) is None:
                        store.unregister_temp(temp_path)
                except (OSError, SyncError):
                    pass

    def _verify_target(self, plan: SyncPlan, expected: dict[str, dict],
                       cancel: threading.Event | None) -> None:
        scanned = _scan(plan.target, cancel, None)
        actual = {_key(path): state for path, state in scanned.items()}
        actual_names = {_key(path): path for path in scanned}
        if actual.keys() != expected.keys():
            raise SyncError("目标目录在同步期间出现或丢失了项目，请重新分析差异。")
        for relative, state in expected.items():
            current = actual[relative]
            matches = identity(current) == identity(state) if state["kind"] == "dir" else current == state
            if not matches:
                raise SyncError(f"目标项目在同步期间发生变化，请重新分析差异：{relative}")
        for source_name in plan.context["source_entries"]:
            if actual_names.get(_key(source_name)) != source_name:
                raise SyncError(f"目标文件名的大小写与源端不同，请重新分析差异：{source_name}")

    def _rename(self, plan: SyncPlan, item: PlanItem, expected: dict[str, dict],
                target_root: dict, cancel: threading.Event | None,
                progress: ProgressCallback, result: SyncResult) -> None:
        _cancelled(cancel)
        relative = item.relative_path
        revalidate_roots(plan.context, target_root=target_root)
        source_path = checked_child(plan.source, relative)
        _require_state(source_path, plan.context["source_entries"][relative], "重命名前源项目已改变")
        old_name = plan.context["target_names"][_key(relative)]
        old_path = checked_child(plan.target, old_name)
        new_path = checked_child(plan.target, relative)
        state = expected[_key(relative)]
        # Parent renames may have changed a directory's mtime. Its identity is
        # the invariant; files must still match the full snapshot.
        if state["kind"] == "dir":
            if identity(snapshot(old_path)) != identity(state) or identity(snapshot(new_path)) != identity(state):
                raise SyncError(f"重命名前目标文件夹已改变：{relative}")
        else:
            _require_state(old_path, state, "重命名前目标文件已改变")
            _require_state(new_path, state, "重命名前目标文件已改变")
        os.rename(native(old_path), native(new_path))
        after = snapshot(new_path)
        if identity(after) != identity(state) or (state["kind"] == "file" and
                (after["size"], after["mtime_ns"]) != (state["size"], state["mtime_ns"])):
            raise SyncError(f"重命名期间目标项目已改变：{relative}")
        expected[_key(relative)] = after
        result.renamed_items += 1
        _emit(progress, "rename", relative_path=relative, message="已统一文件名大小写，无需复制内容")

    def _recover_temps(self, plan: SyncPlan, store: BaselineStore, deletions: list[PlanItem],
                       expected: dict[str, dict], result: SyncResult,
                       cancel: threading.Event | None, progress: ProgressCallback) -> list[PlanItem]:
        """Free crash-left staging space only after the complete preview is checked."""
        planned = {_key(item.relative_path): item for item in deletions if item.action == "delete"}
        recovered: set[str] = set()
        for recorded_path, recorded_identity in store.temps(plan.pair_id):
            _cancelled(cancel)
            path = canonical(recorded_path)
            try:
                relative = os.path.relpath(path, plan.target).replace("\\", "/")
                key = _key(relative)
                item = planned.get(key)
                if item is None or not os.path.basename(path).startswith(".obmanage-") or not path.endswith(".tmp"):
                    continue
                safe_path = checked_child(plan.target, item.relative_path)
                if os.path.normcase(path) != os.path.normcase(safe_path):
                    continue
                revalidate_roots(plan.context)
                if snapshot(checked_child(plan.source, item.relative_path)) is not None:
                    raise SyncError(f"源端出现了暂存文件，已停止清理：{item.relative_path}")
                state = snapshot(safe_path)
                if identity(state) != recorded_identity or state != expected[key]:
                    continue
                os.unlink(native(safe_path))
                store.unregister_temp(recorded_path)
                expected.pop(key)
                recovered.add(key)
                result.deleted_files += 1
                _emit(progress, "delete", relative_path=item.relative_path,
                      message="已清理上次中断留下的已登记暂存文件")
            except ValueError:
                continue
        return [item for item in deletions if _key(item.relative_path) not in recovered]

    def execute(self, plan: SyncPlan, *, allow_empty: bool = False,
                cancel: threading.Event | None = None, progress: ProgressCallback = None) -> SyncResult:
        started = time.monotonic()
        result = SyncResult(status="failed")
        if not self._lock.acquire(blocking=False):
            result.errors.append("已有同步任务运行中。")
            return result
        store: BaselineStore | None = None
        try:
            _cancelled(cancel)
            if not plan.can_execute or not plan.context.get("validated"):
                raise SyncError("当前预览存在错误或不完整，请重新分析差异。")
            context = plan.context
            if (plan.source, plan.target, plan.pair_id) != (context["source"], context["target"], context["pair_id"]):
                raise SyncError("预览路径已改变，请重新分析差异。")
            self._check_state_location(plan.source, plan.target)
            revalidate_roots(context)
            if _scan(plan.source, cancel, progress) != context["source_entries"]:
                raise SyncError("源目录在预览后发生变化，请重新分析差异。")
            if _scan(plan.target, cancel, progress) != context["target_entries"]:
                raise SyncError("目标目录在预览后发生变化，请重新分析差异。")
            deletions = [item for item in plan.items if item.action in ("delete", "rmdir")]
            if plan.source_empty and deletions and not allow_empty:
                raise SyncError("源目录为空。清空目标前必须在界面手动确认。")
            store = BaselineStore(self.state_dir)
            expected_targets = {_key(path): state for path, state in context["target_entries"].items()}
            self._execution_target_states = expected_targets
            deletions = self._recover_temps(plan, store, deletions, expected_targets, result, cancel, progress)
            if plan.bytes_to_copy:
                free = shutil.disk_usage(native(context["target_anchor"])).free
                if free < plan.bytes_to_copy:
                    raise SyncError(f"目标磁盘空间不足：需要 {plan.bytes_to_copy:,} 字节，可用 {free:,} 字节。")
            _cancelled(cancel)
            if context["target_root"] is None:
                revalidate_roots(context)
                os.makedirs(native(plan.target), exist_ok=False)
            target_root = snapshot(plan.target)
            for item in sorted((item for item in plan.items if item.action == "rename"),
                               key=lambda item: (context["source_entries"][item.relative_path]["kind"] != "dir",
                                                 item.relative_path.count("/"), item.relative_path)):
                self._rename(plan, item, expected_targets, target_root, cancel, progress, result)
            for item in sorted((item for item in plan.items if item.action == "mkdir"),
                               key=lambda item: (item.relative_path.count("/"), item.relative_path)):
                _cancelled(cancel)
                revalidate_roots(context, target_root=target_root)
                directory = checked_child(plan.target, item.relative_path)
                if snapshot(directory) is not None:
                    raise SyncError(f"目标目录在预览后已出现：{item.relative_path}")
                os.mkdir(native(directory))
                expected_targets[_key(item.relative_path)] = snapshot(directory)
            for item in (item for item in plan.items if item.action in ("add", "update")):
                _cancelled(cancel)
                copied_state = self._copy_file(plan, item, store, cancel, progress, result, target_root)
                expected_targets[_key(item.relative_path)] = copied_state
            result.skipped_files = plan.counts["skip"]
            _cancelled(cancel)
            # No deletion at all unless all writes succeeded and the entire
            # source is still exactly the snapshot that was previewed.
            revalidate_roots(context, target_root=target_root)
            if _scan(plan.source, cancel, progress) != context["source_entries"]:
                raise SyncError("源目录在同步期间发生变化，已停止删除；请重新分析差异。")
            self._verify_target(plan, expected_targets, cancel)
            # A case-only rename retains content identity. Update its own and
            # descendant skip baselines using the verified post-rename stats.
            if plan.counts.get("rename", 0):
                baselines = store.records(plan.pair_id)
                for item in (item for item in plan.items if item.action == "skip"):
                    record = baselines.get(item.relative_path)
                    if record is not None:
                        store.save(plan.pair_id, item.relative_path,
                                   context["source_entries"][item.relative_path],
                                   expected_targets[_key(item.relative_path)], record[2])
            for item in sorted(deletions, key=lambda item: (item.action == "rmdir", -item.relative_path.count("/"))):
                _cancelled(cancel)
                revalidate_roots(context, target_root=target_root)
                relative = item.relative_path
                source_path = checked_child(plan.source, relative)
                if snapshot(source_path) is not None:
                    raise SyncError(f"源端重新出现此项目，已停止删除：{relative}")
                target_path = checked_child(plan.target, relative)
                expected = context["target_entries"][relative]
                actual = snapshot(target_path)
                if item.action == "delete":
                    if actual != expected:
                        raise SyncError(f"待删除文件在预览后发生变化，已停止删除：{relative}")
                    _mutate_target_file(
                        target_path,
                        expected,
                        "待删除文件在预览后发生变化",
                        lambda: os.unlink(native(target_path)),
                        _WINDOWS_DELETE_READONLY_ERRORS,
                    )
                    result.deleted_files += 1
                    store.remove(plan.pair_id, relative)
                else:
                    if identity(actual) != identity(expected):
                        raise SyncError(f"待删除文件夹已被替换，已停止删除：{relative}")
                    # rmdir only removes an empty, individually checked folder.
                    # Unexpected files inserted by another program are retained.
                    os.rmdir(native(target_path))
                    result.deleted_dirs += 1
                expected_targets.pop(_key(relative))
                _emit(progress, "delete", relative_path=relative, message="已删除目标多余项目",
                      completed_bytes=result.copied_bytes, total_bytes=plan.bytes_to_copy,
                      completed_files=result.deleted_files + result.deleted_dirs, total_files=len(deletions))
            _cancelled(cancel)
            revalidate_roots(context, target_root=target_root)
            if _scan(plan.source, cancel, None) != context["source_entries"]:
                raise SyncError("同步结束时源目录发生变化，本次仅部分完成，请重新分析差异。")
            self._verify_target(plan, expected_targets, cancel)
            result.status = "success"
            _emit(progress, "done", message="镜像同步完成", completed_bytes=result.copied_bytes,
                  total_bytes=plan.bytes_to_copy)
        except SyncCancelled as exc:
            result.status = "cancelled"
            result.errors.append(str(exc))
        except (OSError, SyncError, ValueError, sqlite3.Error) as exc:
            result.status = "failed"
            result.errors.append(str(exc))
        finally:
            if store is not None:
                store.close()
            self.__dict__.pop("_execution_target_states", None)
            result.duration_seconds = time.monotonic() - started
            self._lock.release()
        return result
