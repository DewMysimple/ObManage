from __future__ import annotations

import os
from pathlib import Path
from threading import Event, Lock

import pytest

from obmanage.management import collect_vault_statistics, discover_vaults
from obmanage.management import statistics as statistics_module
from obmanage.models import Progress, SyncCancelled
from obmanage.paths import canonical


def make_vault(path: Path) -> Path:
    (path / ".obsidian").mkdir(parents=True)
    return path


def tree_contents(root: Path) -> tuple[tuple[str, str, bytes | str | None], ...]:
    result: list[tuple[str, str, bytes | str | None]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result.append((relative, "link", os.readlink(path)))
        elif path.is_dir():
            result.append((relative, "dir", None))
        elif path.is_file():
            result.append((relative, "file", path.read_bytes()))
    return tuple(result)


def test_statistics_count_active_markdown_and_exclude_internal_trees(tmp_path):
    parent = make_vault(tmp_path / "Parent")
    root_text = "根目录\n"
    upper_text = "Alpha🙂"
    nested_trash_text = "仍然活动"
    (parent / "root.md").write_bytes(root_text.encode("utf-8"))
    notes = parent / "notes"
    notes.mkdir()
    (notes / "UPPER.MD").write_text(upper_text, encoding="utf-8")
    (notes / "ignored.txt").write_text("ignore", encoding="utf-8")
    nested_trash = notes / ".trash"
    nested_trash.mkdir()
    (nested_trash / "kept.md").write_text(nested_trash_text, encoding="utf-8")
    (parent / ".obsidian" / "config.md").write_text("config", encoding="utf-8")
    root_trash = parent / ".trash"
    root_trash.mkdir()
    (root_trash / "deleted.md").write_text("deleted", encoding="utf-8")
    child = make_vault(parent / "child-vault")
    child_text = "child"
    (child / "child.md").write_text(child_text, encoding="utf-8")
    before = tree_contents(tmp_path)
    catalog = discover_vaults(parent)

    result = collect_vault_statistics(catalog)

    stats = result.by_path
    parent_stats = stats[canonical(parent)]
    assert parent_stats.markdown_files == 3
    assert parent_stats.utf8_characters == len(root_text) + len(upper_text) + len(nested_trash_text)
    assert parent_stats.markdown_bytes == sum(len(text.encode("utf-8")) for text in (
        root_text, upper_text, nested_trash_text
    ))
    assert parent_stats.folders == 2  # notes and notes/.trash
    assert parent_stats.complete
    child_stats = stats[canonical(child)]
    assert child_stats.markdown_files == 1
    assert child_stats.utf8_characters == len(child_text)
    assert child_stats.markdown_bytes == len(child_text.encode("utf-8"))
    assert child_stats.folders == 0
    assert child_stats.complete
    assert result.issues == ()
    assert tree_contents(tmp_path) == before


def test_statistics_excludes_owned_deployment_recovery_directories(tmp_path):
    vault = make_vault(tmp_path / "vault")
    (vault / "active.md").write_text("active", encoding="utf-8")
    recovery = vault / "nested" / ".obmanage-deploy-deadbeef.backup"
    recovery.mkdir(parents=True)
    (recovery / "original.md").write_text("recoverable private data", encoding="utf-8")
    before = tree_contents(tmp_path)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert stats.markdown_files == 1
    assert stats.markdown_bytes == len(b"active")
    assert stats.utf8_characters == len("active")
    assert stats.folders == 1  # "nested" is active; its recovery child is not.
    assert result.issues == ()
    assert tree_contents(tmp_path) == before


def test_statistics_catalog_does_not_reintroduce_deleted_nested_vaults(tmp_path):
    parent = make_vault(tmp_path / "parent")
    (parent / "active.md").write_text("active", encoding="utf-8")
    deleted = make_vault(parent / ".trash" / "deleted-vault")
    (deleted / "deleted.md").write_text("deleted", encoding="utf-8")

    result = collect_vault_statistics(discover_vaults(parent), include_trash=False)

    assert len(result.statistics) == 1
    assert result.statistics[0].vault_path == canonical(parent)
    assert result.statistics[0].markdown_files == 1
    assert result.statistics[0].complete


def test_invalid_utf8_is_counted_as_a_file_and_bytes_but_reported(tmp_path):
    vault = make_vault(tmp_path / "vault")
    good = "good中文"
    (vault / "good.md").write_text(good, encoding="utf-8")
    invalid = b"prefix\xffsuffix"
    (vault / "invalid.md").write_bytes(invalid)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert stats.markdown_files == 2
    assert stats.markdown_bytes == len(good.encode("utf-8")) + len(invalid)
    assert stats.utf8_characters == len(good)
    assert not stats.complete
    assert any(issue.code == "markdown_invalid_utf8" for issue in result.issues)


def test_character_metric_is_strict_utf8_code_points_without_newline_rewriting(tmp_path):
    vault = make_vault(tmp_path / "vault")
    raw = b"\xef\xbb\xbfA\r\n" + "🙂e\u0301".encode("utf-8")
    note = vault / "semantics.md"
    note.write_bytes(raw)
    before = tree_contents(tmp_path)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert stats.utf8_characters == len(raw.decode("utf-8"))
    assert stats.markdown_bytes == len(raw)
    assert stats.characters_counted
    assert stats.complete
    assert tree_contents(tmp_path) == before


def test_character_reads_run_in_bounded_parallel_without_changing_totals(
        tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "vault")
    expected_characters = 0
    for index in range(8):
        text = f"第 {index} 篇🙂" * 200
        expected_characters += len(text)
        (vault / f"note-{index}.md").write_text(text, encoding="utf-8")

    real_read = statistics_module._read_utf8_characters
    guard = Lock()
    two_active = Event()
    active = 0
    maximum_active = 0

    def observed_read(*args, **kwargs):
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
            if active >= 2:
                two_active.set()
        try:
            two_active.wait(2)
            return real_read(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(statistics_module, "_read_utf8_characters", observed_read)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert 2 <= maximum_active <= statistics_module._MAX_CHARACTER_WORKERS
    assert stats.markdown_files == 8
    assert stats.utf8_characters == expected_characters
    assert stats.complete


def test_metadata_only_mode_skips_content_reads_and_trash_is_explicit(tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "vault")
    active = vault / "active.md"
    active.write_bytes(b"active")
    trash = vault / ".trash"
    trash.mkdir()
    deleted = trash / "deleted.md"
    deleted.write_bytes(b"deleted\xff")
    before = tree_contents(tmp_path)

    def unexpected_open(*args, **kwargs):
        raise AssertionError("metadata-only statistics must not open Markdown content")

    monkeypatch.setattr(statistics_module.os, "open", unexpected_open)

    without_trash = collect_vault_statistics(vault, count_characters=False)
    with_trash = collect_vault_statistics(
        vault, count_characters=False, include_trash=True
    )

    excluded = without_trash.statistics[0]
    assert excluded.markdown_files == 1
    assert excluded.markdown_bytes == active.stat().st_size
    assert excluded.utf8_characters == 0
    assert not excluded.characters_counted
    assert excluded.folders == 0
    assert excluded.complete
    included = with_trash.statistics[0]
    assert included.markdown_files == 2
    assert included.markdown_bytes == active.stat().st_size + deleted.stat().st_size
    assert included.utf8_characters == 0
    assert not included.characters_counted
    assert included.folders == 1
    assert included.complete
    assert with_trash.issues == ()
    assert tree_contents(tmp_path) == before


def test_unreadable_markdown_is_not_silent(tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "vault")
    denied = vault / "denied.md"
    denied.write_text("secret", encoding="utf-8")
    real_open = statistics_module.os.open

    def denied_open(path, flags, *args, **kwargs):
        if canonical(path) == canonical(denied):
            raise PermissionError("denied file")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(statistics_module.os, "open", denied_open)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert stats.markdown_files == 1
    assert stats.markdown_bytes == denied.stat().st_size
    assert stats.utf8_characters == 0
    assert not stats.complete
    assert any(issue.code == "markdown_unreadable" and issue.path == canonical(denied)
               for issue in result.issues)


def test_same_size_same_mtime_markdown_change_is_not_reported_complete(tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "vault")
    note = vault / "changed.md"
    note.write_bytes(b"abc")
    original = note.stat()
    real_read = statistics_module.os.read
    changed = False

    def changing_read(descriptor, size):
        nonlocal changed
        block = real_read(descriptor, size)
        if block and not changed:
            changed = True
            note.write_bytes("中".encode("utf-8"))  # Same three-byte length, fewer code points.
            os.utime(note, ns=(original.st_atime_ns, original.st_mtime_ns))
        return block

    monkeypatch.setattr(statistics_module.os, "read", changing_read)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert changed
    assert note.read_text(encoding="utf-8") == "中"
    assert stats.markdown_files == 1
    assert stats.utf8_characters == 0
    assert not stats.complete
    assert any(issue.code == "markdown_unreadable" for issue in result.issues)


def test_late_directory_entry_marks_statistics_incomplete(tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "vault")
    (vault / "first.md").write_text("first", encoding="utf-8")
    real_scandir = statistics_module.os.scandir
    changed = False

    class FrozenScandir:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *_args):
            return False

    def changing_scandir(path):
        nonlocal changed
        iterator = real_scandir(path)
        entries = tuple(iterator)
        iterator.close()
        if str(path).rstrip("\\").casefold().endswith("\\vault") and not changed:
            changed = True
            (vault / "late.md").write_text("late", encoding="utf-8")
        return FrozenScandir(entries)

    monkeypatch.setattr(statistics_module.os, "scandir", changing_scandir)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert changed
    assert len(tuple(vault.glob("*.md"))) == 2
    assert stats.markdown_files == 1
    assert not stats.complete
    assert any(issue.code == "directory_changed" for issue in result.issues)


def test_unreadable_directory_and_special_items_are_reported(tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "vault")
    blocked = vault / "blocked"
    blocked.mkdir()
    (blocked / "hidden.md").write_text("hidden", encoding="utf-8")
    unusual = vault / "unusual"
    unusual.write_bytes(b"device-like")
    real_scandir = statistics_module.os.scandir
    real_snapshot = statistics_module.snapshot

    def denied_scandir(path):
        if canonical(path) == canonical(blocked):
            raise PermissionError("denied directory")
        return real_scandir(path)

    def special_snapshot(path):
        if canonical(path) == canonical(unusual):
            state = real_snapshot(path)
            assert state is not None
            return {**state, "kind": "special"}
        return real_snapshot(path)

    monkeypatch.setattr(statistics_module.os, "scandir", denied_scandir)
    monkeypatch.setattr(statistics_module, "snapshot", special_snapshot)

    result = collect_vault_statistics(vault)

    assert result.statistics[0].folders == 1
    assert not result.statistics[0].complete
    assert {issue.code for issue in result.issues} >= {"directory_unreadable", "unsafe_item"}


def test_markdown_links_are_skipped_and_never_read(tmp_path):
    vault = make_vault(tmp_path / "vault")
    outside = tmp_path / "outside.md"
    outside.write_text("outside private content", encoding="utf-8")
    link = vault / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"File symlinks unavailable: {exc}")
    before = tree_contents(tmp_path)

    result = collect_vault_statistics(vault)

    stats = result.statistics[0]
    assert stats.markdown_files == 0
    assert stats.markdown_bytes == 0
    assert stats.utf8_characters == 0
    assert not stats.complete
    assert any(issue.code == "unsafe_item" and issue.path == canonical(link) for issue in result.issues)
    assert tree_contents(tmp_path) == before


def test_statistics_progress_is_monotonic_and_can_cancel(tmp_path):
    vault = make_vault(tmp_path / "vault")
    (vault / "large.md").write_bytes(("中文" * 700_000).encode("utf-8"))
    updates: list[Progress] = []
    cancel = Event()

    def on_progress(update: Progress) -> None:
        updates.append(update)
        if update.completed_bytes:
            cancel.set()

    with pytest.raises(SyncCancelled):
        collect_vault_statistics(vault, cancel=cancel, progress=on_progress)

    assert updates
    assert all(update.phase == "statistics" for update in updates)
    assert [update.completed_bytes for update in updates] == sorted(
        update.completed_bytes for update in updates
    )
    assert any(update.completed_bytes > 0 for update in updates)
    assert updates[-1].completed_files == 0


def test_non_vault_input_returns_an_explicit_issue(tmp_path):
    directory = tmp_path / "ordinary"
    directory.mkdir()

    result = collect_vault_statistics(directory)

    assert result.statistics == ()
    assert len(result.issues) == 1
    assert result.issues[0].code == "not_a_vault"


def test_catalog_issues_are_preserved_in_statistics_result(tmp_path):
    search = tmp_path / "search"
    vault = make_vault(search / "vault")
    invalid = search / "invalid"
    invalid.mkdir(parents=True)
    (invalid / ".obsidian").write_text("not a directory", encoding="utf-8")
    catalog = discover_vaults(search)
    assert [item.path for item in catalog.vaults] == [canonical(vault)]

    result = collect_vault_statistics(catalog)

    assert any(issue.code == "invalid_marker" for issue in result.issues)
    assert result.statistics[0].complete
