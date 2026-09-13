from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
from threading import Event

import pytest

from obmanage.management import discover_vaults, read_opened_vault_candidates
from obmanage.management import catalog as catalog_module
from obmanage.models import Progress, SyncCancelled
from obmanage.paths import canonical


def tree_contents(root: Path) -> tuple[tuple[str, str, bytes | str | None], ...]:
    """Content-oriented snapshot that intentionally ignores access timestamps."""
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


def make_vault(path: Path) -> Path:
    (path / ".obsidian").mkdir(parents=True)
    return path


def windows_short_alias(path: Path) -> Path:
    from ctypes import wintypes

    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_short.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_short(str(path), buffer, len(buffer))
    if not length:
        pytest.skip(f"Short path lookup unavailable: {ctypes.WinError(ctypes.get_last_error())}")
    alias = Path(buffer.value)
    if os.path.normcase(str(alias)) == os.path.normcase(str(path)):
        pytest.skip("The test volume does not create a distinct 8.3 alias")
    return alias


def test_discovery_is_unique_stable_and_reports_nearest_nested_parent(tmp_path):
    search = tmp_path / "search"
    alpha = make_vault(search / "Alpha")
    nested = make_vault(alpha / "notes" / "Nested")
    zulu = make_vault(search / "Zulu")
    (search / "not-a-vault" / ".obsidian").parent.mkdir(parents=True)
    (search / "not-a-vault" / ".obsidian").write_text("not a directory", encoding="utf-8")
    before = tree_contents(tmp_path)

    result = discover_vaults([zulu, search, alpha])

    assert [item.path for item in result.vaults] == [
        canonical(alpha), canonical(nested), canonical(zulu)
    ]
    assert result.vaults[0].parent_path is None
    assert result.vaults[1].parent_path == canonical(alpha)
    assert result.vaults[1].is_nested
    assert result.vaults[2].parent_path is None
    assert [issue.code for issue in result.issues].count("invalid_marker") == 1
    assert tree_contents(tmp_path) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 aliases are Windows-specific")
def test_discovery_deduplicates_windows_aliases_to_one_physical_path(tmp_path):
    vault = make_vault(tmp_path / "long vault directory name")
    alias = windows_short_alias(vault)
    before = tree_contents(tmp_path)

    result = discover_vaults([alias, vault])

    assert [item.path for item in result.vaults] == [canonical(vault)]
    assert tree_contents(tmp_path) == before


def test_discovery_rejects_links_without_traversing_their_targets(tmp_path):
    search = tmp_path / "search"
    search.mkdir()
    outside = make_vault(tmp_path / "outside")
    (outside / "private.md").write_text("outside", encoding="utf-8")
    link = search / "linked-vault"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")
    before = tree_contents(tmp_path)

    result = discover_vaults(search)

    assert result.vaults == ()
    assert any(issue.code == "unsafe_item" and issue.path == canonical(link) for issue in result.issues)
    assert tree_contents(tmp_path) == before


def test_discovery_never_enters_owned_deployment_transaction_directories(tmp_path):
    search = tmp_path / "search"
    visible = make_vault(search / "visible")
    transaction = search / ".obmanage-deploy-deadbeef.backup"
    hidden_nested = make_vault(transaction / "copied-content" / "nested-vault")

    result = discover_vaults(search)

    assert [item.path for item in result.vaults] == [canonical(visible)]
    assert canonical(hidden_nested) not in {item.path for item in result.vaults}


def test_discovery_never_offers_vaults_inside_a_root_trash(tmp_path):
    parent = make_vault(tmp_path / "parent")
    visible_nested = make_vault(parent / "notes" / "visible")
    deleted = make_vault(parent / ".trash" / "deleted-vault")

    result = discover_vaults(parent)

    assert [item.path for item in result.vaults] == [
        canonical(parent), canonical(visible_nested),
    ]
    assert canonical(deleted) not in {item.path for item in result.vaults}
    assert result.issues == ()


def test_discovery_uses_scandir_metadata_for_ordinary_files(tmp_path, monkeypatch):
    search = tmp_path / "search"
    vault = make_vault(search / "vault")
    ordinary_files = []
    for index in range(40):
        path = search / f"ordinary-{index}.bin"
        path.write_bytes(b"ordinary")
        ordinary_files.append(os.path.normcase(canonical(path)))

    real_snapshot = catalog_module.snapshot
    ordinary_snapshots: list[str] = []

    def observed_snapshot(path):
        key = os.path.normcase(canonical(path))
        if key in ordinary_files:
            ordinary_snapshots.append(key)
        return real_snapshot(path)

    monkeypatch.setattr(catalog_module, "snapshot", observed_snapshot)

    result = discover_vaults(search)

    assert [item.path for item in result.vaults] == [canonical(vault)]
    assert ordinary_snapshots == []


@pytest.mark.parametrize("scope_suffix", [(".trash",), (".trash", "deleted-vault")])
def test_explicit_scope_inside_a_vault_root_trash_is_safely_excluded(tmp_path, scope_suffix):
    parent = make_vault(tmp_path / "parent")
    deleted = make_vault(parent / ".trash" / "deleted-vault")
    scope = parent.joinpath(*scope_suffix)

    result = discover_vaults(scope)

    assert result.vaults == ()
    assert any(issue.code == "trash_scope_excluded" for issue in result.issues)
    assert canonical(deleted) not in {item.path for item in result.vaults}


def test_plain_but_unreadable_marker_is_reported_and_not_a_vault(tmp_path, monkeypatch):
    candidate = make_vault(tmp_path / "candidate")
    real_scandir = catalog_module.os.scandir

    def denied(path):
        if canonical(path) == canonical(candidate / ".obsidian"):
            raise PermissionError("denied marker")
        return real_scandir(path)

    monkeypatch.setattr(catalog_module.os, "scandir", denied)

    result = discover_vaults(candidate)

    assert result.vaults == ()
    assert any(issue.code == "marker_unreadable" for issue in result.issues)


def test_unreadable_subdirectory_is_explicit_and_does_not_stop_other_discovery(tmp_path, monkeypatch):
    search = tmp_path / "search"
    blocked = search / "blocked"
    blocked.mkdir(parents=True)
    visible = make_vault(search / "visible")
    real_scandir = catalog_module.os.scandir

    def denied(path):
        if canonical(path) == canonical(blocked):
            raise PermissionError("denied directory")
        return real_scandir(path)

    monkeypatch.setattr(catalog_module.os, "scandir", denied)

    result = discover_vaults(search)

    assert [item.path for item in result.vaults] == [canonical(visible)]
    assert any(issue.code == "directory_unreadable" and issue.path == canonical(blocked)
               for issue in result.issues)


def test_reads_only_open_obsidian_candidates_and_reports_bad_entries(tmp_path):
    appdata = tmp_path / "appdata"
    config = appdata / "obsidian" / "obsidian.json"
    config.parent.mkdir(parents=True)
    first = tmp_path / "First"
    second = tmp_path / "Second"
    payload = {
        "vaults": {
            "open": {"path": str(second), "open": True},
            "duplicate": {"path": str(second), "open": True},
            "also-open": {"path": str(first), "open": True},
            "closed": {"path": str(tmp_path / "Closed"), "open": False},
            "missing-path": {"open": True},
            "bad-record": "not-an-object",
        }
    }
    config.write_text(json.dumps(payload), encoding="utf-8")
    before = tree_contents(tmp_path)

    result = read_opened_vault_candidates(environ={"APPDATA": str(appdata)})

    assert result.paths == (canonical(first), canonical(second))
    assert [issue.code for issue in result.issues] == [
        "config_invalid_entry", "config_invalid_entry"
    ]
    assert tree_contents(tmp_path) == before


@pytest.mark.parametrize(
    ("contents", "code"),
    [
        ("{broken", "config_invalid_json"),
        (json.dumps({"wrong": {}}), "config_invalid_schema"),
    ],
)
def test_obsidian_config_parse_errors_are_explicit(tmp_path, contents, code):
    config = tmp_path / "obsidian.json"
    config.write_text(contents, encoding="utf-8")

    result = read_opened_vault_candidates(config)

    assert result.paths == ()
    assert len(result.issues) == 1
    assert result.issues[0].code == code
    assert result.issues[0].path == canonical(config)


def test_opened_config_change_during_read_is_reported_as_unreliable(tmp_path, monkeypatch):
    config = tmp_path / "obsidian.json"
    config.write_text(json.dumps({
        "vaults": {"first": {"path": str(tmp_path / "first"), "open": True}}
    }), encoding="utf-8")
    real_read = catalog_module.os.read
    changed = False

    def changing_read(descriptor, size):
        nonlocal changed
        block = real_read(descriptor, size)
        if block and not changed:
            changed = True
            config.write_text(json.dumps({
                "vaults": {"second": {"path": str(tmp_path / "second"), "open": True}}
            }), encoding="utf-8")
        return block

    monkeypatch.setattr(catalog_module.os, "read", changing_read)

    result = read_opened_vault_candidates(config)

    assert changed
    assert result.paths == ()
    assert len(result.issues) == 1
    assert result.issues[0].code == "config_unreadable"
    assert "变化" in result.issues[0].message or "替换" in result.issues[0].message


def test_opened_candidates_are_validated_by_discovery(tmp_path):
    valid = make_vault(tmp_path / "valid")
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    config = tmp_path / "obsidian.json"
    config.write_text(json.dumps({
        "vaults": {
            "valid": {"path": str(valid), "open": True},
            "invalid": {"path": str(invalid), "open": True},
        }
    }), encoding="utf-8")

    result = discover_vaults([], include_opened=True, obsidian_config_path=config)

    assert [item.path for item in result.vaults] == [canonical(valid)]


def test_discovery_progress_and_cancellation_use_existing_contract(tmp_path):
    search = tmp_path / "search"
    (search / "child").mkdir(parents=True)
    cancel = Event()
    updates: list[Progress] = []

    def on_progress(update: Progress) -> None:
        updates.append(update)
        cancel.set()

    with pytest.raises(SyncCancelled):
        discover_vaults(search, cancel=cancel, progress=on_progress)

    assert len(updates) == 1
    assert updates[0].phase == "catalog"
    assert updates[0].completed_files == 1
