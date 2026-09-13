"""Streaming, read-only statistics for active Obsidian vault content."""
from __future__ import annotations

import codecs
import os
import stat
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from threading import Event, Lock

from ..models import Progress, SyncCancelled, SyncError
from ..paths import assert_plain_chain, canonical, native, snapshot
from .catalog import _marker_state, _physical_plain_directory, _same_name, _sort_key
from .models import (
    ManagementIssue,
    VaultCatalogResult,
    VaultInfo,
    VaultStatistics,
    VaultStatisticsResult,
)

ProgressCallback = Callable[[Progress], None]
_READ_SIZE = 1024 * 1024
_PROGRESS_INTERVAL = 0.08
_MAX_CHARACTER_WORKERS = 4
_RESERVED_DIRECTORY_PREFIXES = (".obmanage-deploy-",)


@dataclass
class _MarkdownCandidate:
    path: str
    parent_path: str
    relative_path: str
    name: str
    expected_state: dict
    direct_children: list[tuple[str, bool, tuple | None]]
    direct_child_index: int


def _cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SyncCancelled("操作已取消。")


def _issue_sort_key(issue: ManagementIssue) -> tuple[str, str, str]:
    return ((issue.path or "").casefold(), issue.code, issue.message)


def _state_epoch(state: dict | None, *, content_sensitive: bool = True) -> tuple | None:
    if state is None:
        return None
    identity_epoch = (state["kind"], state["device"], state["inode"])
    if not content_sensitive:
        return identity_epoch
    return identity_epoch + (state["size"], state["mtime_ns"], state["ctime_ns"])


def _stat_epoch(value: os.stat_result) -> tuple:
    kind = "file" if stat.S_ISREG(value.st_mode) else "other"
    return (
        kind, value.st_dev, value.st_ino, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _coerce_vaults(
    vaults: VaultCatalogResult | Iterable[VaultInfo | str | Path] | VaultInfo | str | Path,
) -> list[VaultInfo | str | Path]:
    if isinstance(vaults, VaultCatalogResult):
        return list(vaults.vaults)
    if isinstance(vaults, (VaultInfo, str, Path)):
        return [vaults]
    return list(vaults)


def _read_utf8_characters(
    path: str,
    expected: dict,
    *,
    cancel: Event | None,
    on_bytes: Callable[[int], None],
    parent_validated: bool = False,
) -> tuple[int, dict]:
    """Decode one stable regular file incrementally and return code-point count."""
    if not parent_validated:
        assert_plain_chain(os.path.dirname(path))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    expected_epoch = _state_epoch(expected)
    assert expected_epoch is not None
    descriptor = os.open(native(path), flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("路径不再是普通文件")
        # An open Windows handle and a path lookup can expose slightly different
        # ctime values for a newly-created file.  Compare identity/size/mtime
        # across those APIs, and compare ctime only within each stable API epoch.
        if _stat_epoch(before)[:-1] != expected_epoch[:-1]:
            raise OSError("文件在读取前已发生变化")
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        characters = 0
        while True:
            _cancelled(cancel)
            block = os.read(descriptor, _READ_SIZE)
            if not block:
                break
            characters += len(decoder.decode(block, final=False))
            on_bytes(len(block))
        characters += len(decoder.decode(b"", final=True))
        after = os.fstat(descriptor)
        if _stat_epoch(after) != _stat_epoch(before):
            raise OSError("文件在统计期间发生变化")
    finally:
        os.close(descriptor)
    current = snapshot(path)
    if _state_epoch(current) != expected_epoch:
        raise OSError("文件在统计期间被替换或改变")
    assert current is not None
    return characters, current


def collect_vault_statistics(
    vaults: VaultCatalogResult | Iterable[VaultInfo | str | Path] | VaultInfo | str | Path,
    *,
    count_characters: bool = True,
    include_trash: bool = False,
    cancel: Event | None = None,
    progress: ProgressCallback | None = None,
) -> VaultStatisticsResult:
    """Collect active Markdown statistics for every supplied vault.

    The root ``.obsidian`` tree is always excluded; root ``.trash`` is excluded
    unless ``include_trash`` is true.  A nested directory with its own ordinary
    ``.obsidian`` marker is a hard traversal boundary and is counted separately
    only when it is also supplied as a vault.  Set ``count_characters`` false to
    collect metadata counts without opening and UTF-8 decoding every Markdown
    file; each result then has ``characters_counted == False``.
    """
    issues: list[ManagementIssue] = (
        list(vaults.issues) if isinstance(vaults, VaultCatalogResult) else []
    )
    prepared: dict[str, str] = {}
    for item in _coerce_vaults(vaults):
        _cancelled(cancel)
        raw_path = item.path if isinstance(item, VaultInfo) else item
        physical, issue = _physical_plain_directory(raw_path)
        if issue is not None:
            issues.append(issue)
            continue
        assert physical is not None
        marker_status, marker_issue = _marker_state(physical)
        if marker_issue is not None:
            issues.append(marker_issue)
        if marker_status != "valid":
            if marker_issue is None:
                issues.append(ManagementIssue(
                    "not_a_vault", "目录不包含可用的普通 .obsidian 目录。", physical
                ))
            continue
        prepared.setdefault(os.path.normcase(physical), physical)

    results: list[VaultStatistics] = []
    completed_files = 0
    completed_bytes = 0
    last_progress = 0.0
    last_reported_bytes = 0
    progress_lock = Lock()

    def report(vault_path: str, relative_path: str, *, byte_delta: int = 0,
               file_delta: int = 0, force: bool = False) -> None:
        nonlocal completed_bytes, completed_files, last_progress, last_reported_bytes
        with progress_lock:
            completed_bytes += byte_delta
            completed_files += file_delta
            if progress is None:
                return
            now = time.monotonic()
            first_data = completed_bytes > 0 and last_reported_bytes == 0
            if not force and not first_data and now - last_progress < _PROGRESS_INTERVAL:
                return
            last_progress = now
            last_reported_bytes = completed_bytes
            progress(Progress(
                phase="statistics",
                message=f"正在统计仓库：{Path(vault_path).name or vault_path}",
                relative_path=relative_path,
                completed_bytes=completed_bytes,
                completed_files=completed_files,
            ))

    for vault_path in sorted(prepared.values(), key=_sort_key):
        _cancelled(cancel)
        vault_issues_start = len(issues)
        markdown_files = 0
        markdown_bytes = 0
        utf8_characters = 0
        folders = 0
        stack: list[tuple[str, bool]] = [(vault_path, True)]
        directory_guards: list[
            tuple[str, dict, list[tuple[str, bool, tuple | None]]]
        ] = []
        markdown_candidates: list[_MarkdownCandidate] = []
        changed_directories: set[str] = set()

        def mark_directory_changed(path: str, message: str) -> None:
            key = os.path.normcase(path)
            if key in changed_directories:
                return
            changed_directories.add(key)
            issues.append(ManagementIssue("directory_changed", message, path))

        report(vault_path, "", force=True)

        while stack:
            _cancelled(cancel)
            current, is_root = stack.pop()
            try:
                assert_plain_chain(current)
                folder_before = snapshot(current)
                if folder_before is None or folder_before["kind"] != "dir":
                    raise OSError("目录在统计期间被移除或替换")
                with os.scandir(native(current)) as iterator:
                    entries = sorted(tuple(iterator), key=lambda entry: (entry.name.casefold(), entry.name))
            except (OSError, SyncError) as exc:
                issues.append(ManagementIssue("directory_unreadable", f"无法读取目录：{exc}", current))
                continue

            child_dirs: list[str] = []
            direct_children: list[tuple[str, bool, tuple | None]] = []
            for entry in entries:
                _cancelled(cancel)
                child = canonical(os.path.join(current, entry.name))
                try:
                    state = snapshot(child)
                except OSError as exc:
                    issues.append(ManagementIssue("path_unreadable", f"无法检查路径：{exc}", child))
                    continue
                if state is None:
                    issues.append(ManagementIssue("path_changed", "路径在统计期间消失。", child))
                    direct_children.append((entry.name, True, None))
                    continue
                if state["kind"] in ("link", "special"):
                    direct_children.append((entry.name, False, _state_epoch(state, content_sensitive=False)))
                    issues.append(ManagementIssue(
                        "unsafe_item", "已跳过链接、重解析点或特殊项。", child
                    ))
                    continue
                if state["kind"] == "dir":
                    if _same_name(entry.name, ".obsidian") or (
                        is_root and not include_trash and _same_name(entry.name, ".trash")
                    ) or any(
                        entry.name.casefold().startswith(prefix.casefold())
                        for prefix in _RESERVED_DIRECTORY_PREFIXES
                    ):
                        direct_children.append((
                            entry.name, False, _state_epoch(state, content_sensitive=False)
                        ))
                        continue
                    nested_status, nested_issue = _marker_state(child)
                    if nested_issue is not None:
                        issues.append(nested_issue)
                    if nested_status in ("valid", "unreadable"):
                        # A plain but unreadable marker is conservatively treated
                        # as a nested-vault boundary rather than traversed.
                        direct_children.append((
                            entry.name, False, _state_epoch(state, content_sensitive=False)
                        ))
                        continue
                    direct_children.append((
                        entry.name, False, _state_epoch(state, content_sensitive=False)
                    ))
                    folders += 1
                    child_dirs.append(child)
                    continue
                if state["kind"] != "file" or Path(entry.name).suffix.casefold() != ".md":
                    direct_children.append((
                        entry.name, False, _state_epoch(state, content_sensitive=False)
                    ))
                    continue

                direct_child_index = len(direct_children)
                direct_children.append((entry.name, True, _state_epoch(state)))

                relative = os.path.relpath(child, vault_path)
                markdown_files += 1
                markdown_bytes += state["size"]
                if not count_characters:
                    report(vault_path, relative, file_delta=1, force=True)
                    continue
                markdown_candidates.append(_MarkdownCandidate(
                    path=child,
                    parent_path=current,
                    relative_path=relative,
                    name=entry.name,
                    expected_state=state,
                    direct_children=direct_children,
                    direct_child_index=direct_child_index,
                ))

            try:
                folder_after = snapshot(current)
            except OSError as exc:
                issues.append(ManagementIssue(
                    "directory_unreadable", f"无法复核目录：{exc}", current
                ))
                folder_after = None
            if _state_epoch(folder_after) != _state_epoch(folder_before):
                mark_directory_changed(current, "目录在枚举期间发生变化，统计结果不完整。")
            if folder_after is not None and folder_after["kind"] == "dir":
                directory_guards.append((current, folder_after, direct_children))
            stack.extend((child, False) for child in reversed(child_dirs))

        if count_characters and markdown_candidates:
            def read_candidate(
                candidate: _MarkdownCandidate,
            ) -> tuple[_MarkdownCandidate, int | None, dict | None, Exception | None]:
                try:
                    characters, stable_state = _read_utf8_characters(
                        candidate.path,
                        candidate.expected_state,
                        cancel=cancel,
                        on_bytes=lambda amount: report(
                            vault_path, candidate.relative_path, byte_delta=amount
                        ),
                        parent_validated=True,
                    )
                except (UnicodeDecodeError, OSError, SyncError) as exc:
                    return candidate, None, None, exc
                return candidate, characters, stable_state, None

            worker_count = min(_MAX_CHARACTER_WORKERS, len(markdown_candidates))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="obmanage-statistics",
            ) as executor:
                grouped = groupby(markdown_candidates, key=lambda item: item.parent_path)
                for parent_path, candidate_group in grouped:
                    group = list(candidate_group)
                    try:
                        # Validate the shared chain once immediately before this
                        # directory's files are opened.  Each file handle and path
                        # is still checked independently before and after reading.
                        assert_plain_chain(parent_path)
                        parent_state = snapshot(parent_path)
                        if parent_state is None or parent_state["kind"] != "dir":
                            raise OSError("目录在读取 Markdown 前已被移除或替换")
                    except (OSError, SyncError) as exc:
                        issues.append(ManagementIssue(
                            "directory_unreadable",
                            f"无法安全读取目录中的 Markdown：{exc}",
                            parent_path,
                        ))
                        for candidate in group:
                            report(
                                vault_path,
                                candidate.relative_path,
                                file_delta=1,
                                force=True,
                            )
                        continue

                    outcomes = executor.map(
                        read_candidate,
                        group,
                        buffersize=worker_count * 2,
                    )
                    for candidate, characters, stable_state, error in outcomes:
                        if isinstance(error, UnicodeDecodeError):
                            issues.append(ManagementIssue(
                                "markdown_invalid_utf8",
                                f"Markdown 文件不是有效 UTF-8：{error}",
                                candidate.path,
                            ))
                        elif error is not None:
                            issues.append(ManagementIssue(
                                "markdown_unreadable",
                                f"无法读取 Markdown 文件：{error}",
                                candidate.path,
                            ))
                        else:
                            assert characters is not None and stable_state is not None
                            candidate.direct_children[candidate.direct_child_index] = (
                                candidate.name, True, _state_epoch(stable_state)
                            )
                            utf8_characters += characters
                        # A failed file is completed with an explicit issue.
                        # Cancellation escapes the map iterator, so a partial file
                        # is never reported as complete.
                        report(
                            vault_path,
                            candidate.relative_path,
                            file_delta=1,
                            force=True,
                        )

        # Re-enumerate every traversed directory after all content reads.  This
        # catches late additions/removals and replacements that a single scandir
        # snapshot would otherwise omit from an apparently exact result.
        for directory, expected_folder, expected_children in directory_guards:
            _cancelled(cancel)
            try:
                assert_plain_chain(directory)
                final_before = snapshot(directory)
                if _state_epoch(final_before) != _state_epoch(expected_folder):
                    mark_directory_changed(
                        directory, "目录在统计期间发生变化，统计结果不完整。"
                    )
                    continue
                with os.scandir(native(directory)) as iterator:
                    final_entries = sorted(
                        tuple(iterator), key=lambda entry: (entry.name.casefold(), entry.name)
                    )
                final_children: list[tuple[str, dict | None]] = []
                for entry in final_entries:
                    child = canonical(os.path.join(directory, entry.name))
                    final_children.append((entry.name, snapshot(child)))
                final_after = snapshot(directory)
            except (OSError, SyncError) as exc:
                issues.append(ManagementIssue(
                    "directory_unreadable", f"无法复核目录：{exc}", directory
                ))
                continue

            changed = _state_epoch(final_after) != _state_epoch(final_before)
            if len(final_children) != len(expected_children):
                changed = True
            elif not changed:
                for (actual_name, actual_state), (expected_name, sensitive, expected_state) in zip(
                    final_children, expected_children
                ):
                    if (actual_name != expected_name
                            or _state_epoch(actual_state, content_sensitive=sensitive) != expected_state):
                        changed = True
                        break
            if changed:
                mark_directory_changed(
                    directory, "目录在统计期间的项目或身份已变化，统计结果不完整。"
                )

        results.append(VaultStatistics(
            vault_path=vault_path,
            markdown_files=markdown_files,
            utf8_characters=utf8_characters,
            markdown_bytes=markdown_bytes,
            folders=folders,
            characters_counted=count_characters,
            complete=len(issues) == vault_issues_start,
        ))

    return VaultStatisticsResult(
        statistics=tuple(results),
        issues=tuple(sorted(issues, key=_issue_sort_key)),
    )
