"""Read-only vault snapshots published as verified ZIPs outside the source tree."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..copying import copy_stream_and_hash
from ..file_types import is_video_filename
from ..models import Progress, SyncCancelled, SyncError
from ..paths import (assert_plain_chain, canonical, checked_child, identity, native,
                     snapshot, validate_state_separation, volume_identity)
from ..reading import hash_stream
from .recovery import require_recovery_clear


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    state: dict
    excluded: str = ""
    digest: str = ""


@dataclass(frozen=True)
class ArchivePlan:
    source: str
    output: str
    level: int
    exclude_videos: bool
    entries: tuple[ArchiveEntry, ...]
    source_state: dict
    output_state: dict
    source_volume: str
    output_volume: str

    @property
    def files(self):
        return tuple(e for e in self.entries if e.state["kind"] == "file" and not e.excluded)

    @property
    def total_bytes(self):
        return sum(e.state["size"] for e in self.files)


@dataclass(frozen=True)
class ArchiveResult:
    output: str
    files: int
    source_bytes: int
    archive_bytes: int
    duration: float


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("已取消打包。")


def _emit(progress, phase, message, path="", done=0, total=0):
    if progress:
        progress(Progress(phase, message, path, done, total))


def _resolve_directory(raw):
    if not str(raw).strip():
        raise SyncError("请选择来源和保存文件夹。")
    assert_plain_chain(raw)
    path = canonical(os.path.realpath(native(raw)))
    state = snapshot(path)
    if state is None or state["kind"] != "dir" or not state["inode"]:
        raise SyncError(f"文件夹不存在或无法核验身份：{path}")
    return path, state


def _read_entry(source, entry, cancel, destination=None, progress=None):
    path = checked_child(source, entry.path)
    if snapshot(path) != entry.state:
        raise SyncError(f"来源已改变，请重新预览：{entry.path}")
    expected = entry.state
    with open(native(path), "rb", buffering=0) as stream:
        opened = os.fstat(stream.fileno())
        actual = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        # On Windows Python's path stat and handle stat can expose different
        # ctime semantics. Compare their common identity/size/mtime fields;
        # retain ctime within each API's before/after checks.
        if not stat.S_ISREG(opened.st_mode) or actual[:4] != tuple(expected[k] for k in
                ("device", "inode", "size", "mtime_ns")):
            raise SyncError(f"打开来源时身份改变：{entry.path}")
        if destination is None:
            count, digest = hash_stream(stream, expected["size"], check_cancel=lambda: _cancel(cancel), progress=progress)
        else:
            count, digest = copy_stream_and_hash(stream, destination, expected["size"],
                                                 check_cancel=lambda: _cancel(cancel), progress=progress)
        finished = os.fstat(stream.fileno())
        if actual != (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns, finished.st_ctime_ns):
            raise SyncError(f"读取期间来源改变：{entry.path}")
    if count != expected["size"] or snapshot(checked_child(source, entry.path)) != expected:
        raise SyncError(f"读取后来源改变：{entry.path}")
    return digest


def _inventory(source, exclude_videos, cancel):
    rows = []
    stack = [(source, "")]
    seen = set()
    while stack:
        _cancel(cancel)
        folder, prefix = stack.pop()
        assert_plain_chain(folder)
        before = snapshot(folder)
        if before is None or before["kind"] != "dir":
            raise SyncError(f"目录已改变：{folder}")
        with os.scandir(native(folder)) as iterator:
            children = sorted(iterator, key=lambda e: e.name.casefold())
        for child in children:
            _cancel(cancel)
            relative = prefix + child.name
            path = checked_child(source, relative)
            state = snapshot(path)
            if state is None or state["kind"] not in {"dir", "file"}:
                raise SyncError(f"项目已消失或不是普通文件/目录：{relative}")
            if relative.casefold() in seen:
                raise SyncError(f"存在大小写重名：{relative}")
            seen.add(relative.casefold())
            reason = ("事务恢复目录" if child.name.casefold().startswith(".obmanage-deploy-") else
                      "视频" if state["kind"] == "file" and exclude_videos and is_video_filename(child.name) else "")
            rows.append(ArchiveEntry(relative, state, reason))
            if state["kind"] == "dir" and not reason:
                stack.append((path, relative + "/"))
        if snapshot(folder) != before:
            raise SyncError(f"枚举期间目录改变：{folder}")
    return tuple(sorted(rows, key=lambda e: e.path))


def _same_inventory(first, second):
    return tuple((e.path, e.state, e.excluded) for e in first) == tuple((e.path, e.state, e.excluded) for e in second)


def _verify_zip(path, entries, cancel, progress=None):
    expected = {e.path + ("/" if e.state["kind"] == "dir" else ""): e for e in entries if not e.excluded}
    total = sum(e.state["size"] for e in expected.values())
    done = 0
    with zipfile.ZipFile(native(path)) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != set(expected):
            raise SyncError("压缩包的文件或目录清单不一致。")
        for name, entry in expected.items():
            _cancel(cancel)
            if entry.state["kind"] == "dir":
                if not archive.getinfo(name).is_dir() or archive.getinfo(name).file_size:
                    raise SyncError(f"压缩包目录结构不一致：{name}")
                continue
            with archive.open(name) as stream:
                count, digest = hash_stream(stream, entry.state["size"], check_cancel=lambda: _cancel(cancel),
                    progress=lambda n: _emit(progress, "verify", "正在校验压缩包", name, done + n, total))
            if count != entry.state["size"] or digest != entry.digest:
                raise SyncError(f"压缩包内容校验失败：{name}")
            done += count


class ArchiveEngine:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)

    def analyze(self, source, output_dir, *, filename="", level=1, exclude_videos=False, cancel=None, progress=None):
        if type(level) is not int or not 0 <= level <= 9:
            raise SyncError("压缩级别必须为 0 至 9。")
        source, source_state = _resolve_directory(source)
        if any(part.casefold().startswith(".obmanage-deploy-") for part in Path(source).parts):
            raise SyncError("不能打包应用的事务恢复目录。")
        output_dir, output_state = _resolve_directory(output_dir)
        validate_state_separation(self.state_dir, (source,))
        # The destination may be an ancestor (e.g. Desktop), but never inside the source.
        try:
            inside = os.path.commonpath((os.path.normcase(source), os.path.normcase(output_dir))) == os.path.normcase(source)
        except ValueError:
            inside = False
        if inside:
            raise SyncError("压缩包必须保存到来源文件夹之外。")
        if not filename:
            filename = f"{Path(source).name}_{datetime.now():%Y%m%d_%H%M%S}.zip"
        if (Path(filename).name != filename or any(c in filename for c in '<>:"/\\|?*')
                or not filename.lower().endswith(".zip") or filename.endswith((" ", "."))):
            raise SyncError("请输入不含路径或特殊字符的 .zip 文件名。")
        output = checked_child(output_dir, filename)
        if snapshot(output) is not None:
            raise SyncError("同名压缩包已存在，请更换文件名；不会覆盖旧包。")
        source_volume, output_volume = volume_identity(source), volume_identity(output_dir)
        _emit(progress, "scan", "正在读取打包清单")
        raw = _inventory(source, exclude_videos, cancel)
        if not any(e.state["kind"] == "dir" and Path(e.path).name.casefold() == ".obsidian"
                   and not e.excluded and ".trash" not in [p.casefold() for p in Path(e.path).parts[:-1]] for e in raw):
            raise SyncError("来源中未发现 .obsidian 仓库标记，请选择仓库或仓库集合。")
        rows = []
        total = sum(e.state["size"] for e in raw if not e.excluded)
        done = 0
        for entry in raw:
            digest = ""
            if entry.state["kind"] == "file" and not entry.excluded:
                digest = _read_entry(source, entry, cancel, progress=lambda n:
                    _emit(progress, "scan", "正在生成打包预览", entry.path, done + n, total))
                done += entry.state["size"]
            rows.append(ArchiveEntry(entry.path, entry.state, entry.excluded, digest))
        plan = ArchivePlan(source, output, level, bool(exclude_videos), tuple(rows), source_state,
                           output_state, source_volume, output_volume)
        self._validate(plan, cancel)
        return plan

    def _validate(self, plan, cancel):
        validate_state_separation(self.state_dir, (plan.source,))
        for path, state, volume in ((plan.source, plan.source_state, plan.source_volume),
                                    (str(Path(plan.output).parent), plan.output_state, plan.output_volume)):
            assert_plain_chain(path)
            if identity(snapshot(path)) != identity(state) or volume_identity(path) != volume:
                raise SyncError("来源或保存位置的磁盘/目录身份改变，请重新预览。")
        if snapshot(checked_child(str(Path(plan.output).parent), Path(plan.output).name)) is not None:
            raise SyncError("输出位置已有同名文件，已停止，旧文件保持不变。")
        if not _same_inventory(plan.entries, _inventory(plan.source, plan.exclude_videos, cancel)):
            raise SyncError("来源清单已改变，请重新预览。")

    def execute(self, plan, *, cancel=None, progress=None):
        require_recovery_clear(self.state_dir)
        _cancel(cancel)
        self._validate(plan, cancel)
        directory = str(Path(plan.output).parent)
        files, total_bytes = plan.files, plan.total_bytes
        if shutil.disk_usage(native(directory)).free < total_bytes + len(plan.entries) * 512 + 1024 * 1024:
            raise SyncError("保存位置可用空间不足；请预留未压缩大小及 ZIP 目录空间。")
        started = time.monotonic()
        temporary = None
        temp_identity = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".obmanage-archive-", suffix=".partial", dir=native(directory))
            temp_identity = identity(snapshot(temporary))
            compression = zipfile.ZIP_STORED if plan.level == 0 else zipfile.ZIP_DEFLATED
            done = 0
            with os.fdopen(descriptor, "w+b") as staged:
                with zipfile.ZipFile(staged, "w", compression=compression, compresslevel=plan.level or None, allowZip64=True) as archive:
                    for entry in plan.entries:
                        _cancel(cancel)
                        if entry.excluded:
                            continue
                        if entry.state["kind"] == "dir":
                            archive.writestr(entry.path + "/", b"")
                            continue
                        date = time.localtime(entry.state["mtime_ns"] / 1e9)[:6]
                        info = zipfile.ZipInfo(entry.path, date if 1980 <= date[0] <= 2107 else (1980, 1, 1, 0, 0, 0))
                        info.compress_type = compression
                        info.compress_level = plan.level or None
                        info.file_size = entry.state["size"]
                        with archive.open(info, "w", force_zip64=True) as destination:
                            digest = _read_entry(plan.source, entry, cancel, destination, lambda n:
                                _emit(progress, "archive", "正在打包", entry.path, done + n, total_bytes))
                        if digest != entry.digest:
                            raise SyncError(f"来源内容与预览不一致：{entry.path}")
                        done += entry.state["size"]
                staged.flush()
                os.fsync(staged.fileno())
            verified_state = snapshot(temporary)
            if identity(verified_state) != temp_identity:
                raise SyncError("临时压缩包身份改变，已停止校验。")
            _verify_zip(temporary, plan.entries, cancel, progress)
            # Re-read the source after writing: the archive must still represent the preview.
            for entry in files:
                _emit(progress, "source_verify", "正在复核来源", entry.path)
                if _read_entry(plan.source, entry, cancel) != entry.digest:
                    raise SyncError(f"打包期间来源内容改变：{entry.path}")
            self._validate(plan, cancel)
            require_recovery_clear(self.state_dir)
            _cancel(cancel)
            if snapshot(temporary) != verified_state:
                raise SyncError("临时压缩包在校验后改变，已停止发布。")
            size = os.path.getsize(temporary)
            if os.name == "nt":
                os.rename(temporary, native(plan.output))  # Windows rename never overwrites.
            else:
                os.link(temporary, native(plan.output))  # Exclusive publication on POSIX too.
                os.unlink(temporary)
            temporary = None
            _emit(progress, "done", "打包完成，完整性校验通过", done=total_bytes, total=total_bytes)
            return ArchiveResult(plan.output, len(files), total_bytes, size, time.monotonic() - started)
        finally:
            if temporary is not None:
                assert_plain_chain(directory)
                if identity(snapshot(directory)) == identity(plan.output_state) and identity(snapshot(temporary)) == temp_identity:
                    os.unlink(temporary)
