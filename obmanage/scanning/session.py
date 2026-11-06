"""Bounded-lifetime directory observations, shared only inside one task epoch.

No DirEntry survives enumeration. Windows DirEntry.stat has zero inode/device;
only full path snapshots can enter a mirror manifest. A fresh session is required
for final revalidation: a cache hit is never evidence that a tree stayed unchanged.
"""
from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from threading import Event
from types import MappingProxyType
from typing import Mapping

from ..file_types import is_video_filename
from ..models import SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, is_reparse, native, snapshot
from .io import scandir
from .metrics import count, current, measured


@dataclass(frozen=True)
class ScanIssue:
    code: str
    path: str
    message: str


class ScanPathError(SyncError):
    """A directory could not be entered; discovery keeps its existing codes."""

    def __init__(self, code, path, message):
        super().__init__(message)
        self.code, self.path = code, path


@dataclass(frozen=True)
class ObservedEntry:
    name: str
    state: Mapping | None



@dataclass(frozen=True)
class DirectoryObservation:
    path: str
    state: Mapping
    entries: tuple[ObservedEntry, ...]
    full_metadata: bool
    epoch: str
    issues: tuple[ScanIssue, ...] = ()

    def name_conflicts(self):
        """Business adapters decide which namespaces an ambiguity affects."""
        seen = set()
        for entry in self.entries:
            if (entry.state is not None and entry.state["kind"] == "dir"
                    and entry.name.casefold().startswith(".obmanage-deploy-")):
                continue
            key = os.path.normcase(entry.name)
            if key in seen:
                yield entry.name
            seen.add(key)


@dataclass(frozen=True)
class ManifestView:
    root: str
    root_state: Mapping | None
    entries: Mapping[str, Mapping]
    excluded: Mapping[str, Mapping]
    issues: tuple[ScanIssue, ...]
    epoch: str
    policy: str

    @property
    def complete(self):
        return not self.issues

    def require_complete(self):
        if self.issues:
            raise SyncError("无法完整扫描：" + "；".join(i.message for i in self.issues))
        return self

    def dictionaries(self):
        self.require_complete()
        return ({p: dict(s) for p, s in self.entries.items()},
                {p: dict(s) for p, s in self.excluded.items()})


def _cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("操作已取消。")


def _enumerated_state(value):
    kind = ("link" if is_reparse(value) else "dir" if stat.S_ISDIR(value.st_mode)
            else "file" if stat.S_ISREG(value.st_mode) else "special")
    return dict(kind=kind, size=value.st_size if kind == "file" else 0,
                mtime_ns=value.st_mtime_ns, ctime_ns=value.st_ctime_ns,
                inode=value.st_ino, device=value.st_dev)


class ScanSession:
    """An explicit observation epoch; caller owns all content and write checks."""

    def __init__(self, cancel: Event | None = None, *, capture=False):
        self.cancel = cancel
        self.capture = capture
        self.epoch = uuid.uuid4().hex
        self.directories: dict[str, DirectoryObservation] = {}
        self.vault_roots: set[str] = set()
        self.markers: dict[str, tuple] = {}
        self.discovery_keys: set[str] = set()
        self.closed = False

    def __enter__(self):
        if self.closed:
            raise RuntimeError("Scan session already closed")
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.directories.clear()
        self.vault_roots.clear()
        self.markers.clear()
        self.discovery_keys.clear()
        self.closed = True

    def inside_vault(self, path):
        path = os.path.normcase(canonical(path))
        while True:
            if path in self.vault_roots:
                return True
            parent = os.path.dirname(path)
            if parent == path:
                return False
            path = parent

    def verify_discovery(self, verified_directories):
        """Check discovery-only regions not covered by a business final scan.

        Regular file content outside vaults is irrelevant; directory/marker/link
        names and root identities determine the catalog's discovery boundary.
        """
        def shape(observation):
            return tuple((e.name, e.state["kind"] if e.state else None)
                for e in observation.entries if e.state is None or e.state["kind"] != "file"
                or os.path.normcase(e.name) == os.path.normcase(".obsidian"))
        issues = []
        with ScanSession(self.cancel) as fresh:
            for key in sorted(self.discovery_keys - verified_directories):
                _cancel(self.cancel)
                original = self.directories[key]
                try:
                    live = fresh.observe(original.path, full=False)
                    fields = ("kind", "device", "inode")
                    if (live.issues or shape(live) != shape(original)
                            or any(live.state[f] != original.state[f] for f in fields)):
                        raise SyncError("仓库发现范围或目录身份在统计期间改变。")
                except (OSError, SyncError) as exc:
                    issues.append(ScanIssue("discovery_changed", original.path, str(exc)))
        return tuple(issues)

    @measured("inventory")
    def observe(self, directory, *, full=True, snapshotter=None,
                chain_validated=False, folder_state=None, entry_snapshotter=None):
        if self.closed:
            raise RuntimeError("Scan session already closed")
        _cancel(self.cancel)
        directory = canonical(directory)
        key = os.path.normcase(directory)
        previous = self.directories.get(key)
        if previous is not None and (previous.full_metadata or not full):
            count("reused_directories")
            count("reused_entries", len(previous.entries))
            return previous
        read_state = snapshotter or snapshot
        try:
            before = folder_state
            if not chain_validated:
                checked = assert_plain_chain(directory, leaf_snapshotter=read_state if before is None else None,
                                             attributes_only_ancestors=True)
                if before is None:
                    before = checked
            elif before is None:
                before = read_state(directory)
        except (OSError, SyncError) as exc:
            raise ScanPathError("unsafe_path", directory, f"无法安全检查目录：{exc}") from exc
        if before is None:
            raise ScanPathError("missing_path", directory, "目录不存在。")
        if before["kind"] != "dir":
            raise ScanPathError("unsafe_path", directory, "路径不是普通目录或包含链接。")
        children = []
        entry_issues = [] if previous is None else list(previous.issues)
        if previous is None:
            with scandir(native(directory)) as iterator:
                for entry in iterator:
                    _cancel(self.cancel)
                    path = os.path.join(directory, entry.name)
                    try:
                        if full:
                            state = (entry_snapshotter(path) if entry_snapshotter is not None
                                     else read_state(path))
                        else:
                            metrics = current()
                            value = (entry.stat(follow_symlinks=False) if metrics is None else
                                     metrics.query("direntry_stat", entry.stat, follow_symlinks=False))
                            state = _enumerated_state(value)
                    except FileNotFoundError:
                        state = None
                    except OSError as exc:
                        entry_issues.append(ScanIssue("path_unreadable", path, f"无法检查路径：{exc}"))
                        continue
                    if state is None:
                        entry_issues.append(ScanIssue("path_changed", path, "路径在扫描期间消失。"))
                    children.append(ObservedEntry(entry.name, MappingProxyType(state) if state else None))
        else:
            # Upgrade discovery metadata, without another enumeration or cached
            # DirEntry.stat. The final fresh epoch detects additions/removals.
            if before != previous.state:
                raise SyncError(f"目录在采集清单前改变：{directory}")
            count("reused_directories")
            count("metadata_upgrades")
            for entry in previous.entries:
                _cancel(self.cancel)
                if entry.state is None:
                    children.append(entry)
                    continue
                state = read_state(os.path.join(directory, entry.name))
                # Enumeration timestamps can lag path lookups on Windows while
                # newly closed descendants flush metadata. This is the FIRST
                # authoritative full snapshot, not an equality/cache claim.
                # Final validation still uses a fresh full observation epoch.
                if state is None or state["kind"] != entry.state["kind"]:
                    raise SyncError(f"路径在采集清单前改变：{entry.name}")
                children.append(ObservedEntry(entry.name, MappingProxyType(state)))
        _cancel(self.cancel)
        after = read_state(directory)
        if after is None or after["kind"] != "dir":
            raise SyncError(f"目录在枚举期间消失或被替换：{directory}")
        issues = tuple(entry_issues) + (() if before == after else
                  (ScanIssue("directory_changed", directory, "目录在枚举期间发生变化，扫描结果不完整。"),))
        observation = DirectoryObservation(directory, MappingProxyType(after),
            tuple(sorted(children, key=lambda e: (e.name.casefold(), e.name))), full, self.epoch, issues)
        self.directories[key] = observation
        return observation

    def manifest(self, root, *, policy="mirror", exclude_videos=False, progress=None):
        if policy not in ("mirror", "no_video", "archive"):
            raise ValueError("Unknown manifest policy")
        root = canonical(root)
        _cancel(self.cancel)
        root_state = snapshot(root)
        entries, excluded, issues = {}, {}, []
        if root_state is None:
            return ManifestView(root, None, MappingProxyType(entries), MappingProxyType(excluded),
                                (), self.epoch, policy)
        stack = [(root, "")]
        seen = set()
        while stack:
            _cancel(self.cancel)
            folder, prefix = stack.pop()
            try:
                observed = self.observe(folder)
                issues.extend(observed.issues)
                for entry in observed.entries:
                    _cancel(self.cancel)
                    relative = prefix + entry.name
                    state = entry.state
                    if state is None:
                        continue
                    if state["kind"] not in ("file", "dir"):
                        raise SyncError(f"不支持链接、重解析点或特殊文件：{relative}")
                    reserved = state["kind"] == "dir" and entry.name.casefold().startswith(".obmanage-deploy-")
                    if reserved and policy != "archive":
                        continue
                    key = relative.casefold() if policy == "archive" else os.path.normcase(relative)
                    if key in seen:
                        raise SyncError(f"存在 Windows 无法区分的大小写重名路径：{relative}")
                    seen.add(key)
                    video = (policy == "no_video" or exclude_videos) and state["kind"] == "file" and is_video_filename(entry.name)
                    if reserved or video:
                        excluded[relative] = state
                    else:
                        entries[relative] = state
                        if state["kind"] == "dir":
                            stack.append((os.path.join(folder, entry.name), relative + "/"))
                    if progress is not None and len(entries) % 100 == 0:
                        progress(relative, len(entries))
            except (OSError, SyncError) as exc:
                issues.append(ScanIssue("incomplete_scan", folder, str(exc)))
        return ManifestView(root, MappingProxyType(root_state), MappingProxyType(entries),
                            MappingProxyType(excluded), tuple(issues), self.epoch, policy)
