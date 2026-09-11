from __future__ import annotations

import os
import ctypes
from pathlib import Path

import pytest

from obmanage.engine import SyncEngine
from obmanage.models import SyncError


def rejected(engine, source, target):
    try:
        plan = engine.analyze(str(source), str(target))
    except SyncError:
        return
    assert not plan.can_execute, "Unsafe paths must be rejected before synchronization"


def test_rejects_identical_source_and_target(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    rejected(SyncEngine(tmp_path / "state"), vault, vault)


@pytest.mark.parametrize("source_inside_target", [False, True])
def test_rejects_overlapping_source_and_target(tmp_path, source_inside_target):
    outer = tmp_path / "vault"
    inner = outer / "mirror"
    inner.mkdir(parents=True)
    source, target = (inner, outer) if source_inside_target else (outer, inner)
    rejected(SyncEngine(tmp_path / "state"), source, target)


def test_rejects_target_drive_root(tmp_path):
    source = tmp_path / "vault"
    source.mkdir()
    rejected(SyncEngine(tmp_path / "state"), source, Path(tmp_path.anchor))


def test_rejects_lexically_disguised_same_path(tmp_path):
    source = tmp_path / "vault"
    (source / "child").mkdir(parents=True)
    rejected(SyncEngine(tmp_path / "state"), source, source / "child" / "..")


@pytest.mark.parametrize("state_side", ["source", "target"])
def test_state_database_cannot_be_created_inside_either_vault(tmp_path, state_side):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    state = (source if state_side == "source" else target) / "app-state"

    rejected(SyncEngine(state), source, target)

    assert not state.exists()


@pytest.mark.parametrize("repo_side", ["source", "target"])
def test_vault_cannot_be_nested_inside_state_directory(tmp_path, repo_side):
    state = tmp_path / "state"
    state.mkdir()
    nested = state / "vault"
    nested.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    source, target = (nested, other) if repo_side == "source" else (other, nested)

    rejected(SyncEngine(state), source, target)

    assert not (state / "baselines.sqlite3").exists()


def test_missing_target_can_be_previewed_without_creating_it(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_bytes(b"hello")
    target = tmp_path / "not-yet-created" / "vault"
    engine = SyncEngine(tmp_path / "state")

    plan = engine.analyze(str(source), str(target))

    assert plan.can_execute, plan.errors
    assert plan.counts["add"] == 1
    assert not target.exists()
    result = engine.execute(plan)
    assert not result.errors, result.errors
    assert (target / "note.md").read_bytes() == b"hello"


@pytest.mark.parametrize("link_side", ["source", "target"])
def test_directory_links_never_traverse_outside_vault(tmp_path, link_side):
    source, target, outside = [tmp_path / name for name in ("source", "target", "outside")]
    for folder in (source, target, outside):
        folder.mkdir()
    (source / "note.md").write_bytes(b"source")
    (outside / "private.md").write_bytes(b"outside original")
    link = (source if link_side == "source" else target) / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")

    engine = SyncEngine(tmp_path / "state")
    rejected(engine, source, target)

    assert (outside / "private.md").read_bytes() == b"outside original"
    assert not (target / "link" / "note.md").exists()


def test_long_unicode_paths_can_be_mirrored(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    relative = Path(*(["中文长路径" * 5] * 8), "深层笔记.md")
    source_file = source / relative
    raw_path = str(source_file.absolute())
    extended_source = Path("\\\\?\\" + raw_path) if os.name == "nt" else source_file
    extended_source.parent.mkdir(parents=True)
    extended_source.write_bytes("中文内容".encode())
    assert len(str(source_file)) > 260
    engine = SyncEngine(tmp_path / "state")

    plan = engine.analyze(str(source), str(target))

    assert plan.can_execute, plan.errors
    result = engine.execute(plan)
    assert not result.errors, result.errors
    target_file = target / relative
    extended_target = Path("\\\\?\\" + str(target_file.absolute())) if os.name == "nt" else target_file
    assert extended_target.read_bytes() == "中文内容".encode()


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
        pytest.skip("The test volume does not create distinct Windows 8.3 path aliases")
    return alias


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 path aliases are Windows-specific")
def test_rejects_same_vault_addressed_by_windows_short_alias(tmp_path):
    vault = tmp_path / "long vault directory name"
    vault.mkdir()
    alias = windows_short_alias(vault)

    rejected(SyncEngine(tmp_path / "state"), alias, vault)


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 path aliases are Windows-specific")
@pytest.mark.parametrize("source_inside_target", [False, True])
def test_rejects_nested_vaults_disguised_by_windows_short_alias(tmp_path, source_inside_target):
    outer = tmp_path / "long vault directory name"
    inner = outer / "mirror"
    inner.mkdir(parents=True)
    alias = windows_short_alias(outer)
    source, target = (inner, alias) if source_inside_target else (alias, inner)

    rejected(SyncEngine(tmp_path / "state"), source, target)


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 path aliases are Windows-specific")
def test_state_directory_cannot_hide_inside_vault_using_windows_short_alias(tmp_path):
    source = tmp_path / "long vault directory name"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    alias = windows_short_alias(source)
    state = alias / "app-state"

    rejected(SyncEngine(state), source, target)

    assert not (source / "app-state").exists()
