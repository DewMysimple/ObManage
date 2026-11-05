"""Compare against immutable 2.7.1 Git source, not a second call to new code."""
import os
from pathlib import Path

import pytest

from obmanage.engine import _scan
from obmanage.management.catalog import discover_vaults
from obmanage.management.statistics import collect_vault_statistics
from obmanage.scanning.metrics import ScanMetrics
from obmanage.scanning.session import ScanSession
from tools.benchmark_scanning import reference_package, signature


@pytest.fixture(scope="module")
def reference():
    # The reference is read from an immutable commit; no production copy is kept.
    with reference_package() as modules:
        yield modules


def dataset(root):
    for relative in ("甲", "two/甲", "甲/nested", "甲/.trash/deleted", ".obmanage-deploy-test/hidden"):
        vault = root / relative
        (vault / ".obsidian").mkdir(parents=True)
        (vault / ".obsidian/app.json").write_bytes(b"{}")
        (vault / "notes").mkdir()
        (vault / "notes/中文🙂.md").write_text("中🙂\r\nx" * 100, encoding="utf8")
        (vault / "clip.MP4").write_bytes(b"video")
        (vault / "empty").mkdir()
    (root / "ordinary/subdir").mkdir(parents=True)
    (root / "ordinary/data.bin").write_bytes(b"ordinary")
    (root / "invalid").mkdir()
    (root / "invalid/.obsidian").write_bytes(b"file marker")


@pytest.mark.parametrize("scope", [".", "甲", "ordinary", "missing"])
def test_discovery_structured_result_matches_reference(tmp_path, reference, scope):
    dataset(tmp_path)
    old = reference["management.catalog"].discover_vaults(tmp_path / scope)
    new = discover_vaults(tmp_path / scope)
    assert signature(old) == signature(new)


@pytest.mark.parametrize("mode", ["mirror", "no_video"])
def test_full_snapshot_fields_and_exclusions_match_reference(tmp_path, reference, mode):
    dataset(tmp_path)
    old = reference["engine"]._scan(str(tmp_path / "甲"), None, None, mode=mode)
    with ScanSession() as session:
        view = session.manifest(tmp_path / "甲", policy=mode)
        assert view.complete
        assert old == view.dictionaries()
        assert "nested/notes/中文🙂.md" in view.entries
        assert ".trash/deleted/notes/中文🙂.md" in view.entries


@pytest.mark.parametrize("characters", [False, True])
@pytest.mark.parametrize("trash", [False, True])
def test_discovery_and_statistics_share_observations_with_identical_totals(tmp_path, reference, characters, trash):
    dataset(tmp_path)
    old_catalog = reference["management.catalog"].discover_vaults(tmp_path)
    old = reference["management.statistics"].collect_vault_statistics(
        old_catalog, count_characters=characters, include_trash=trash)
    metrics = ScanMetrics()
    with metrics.activate(), ScanSession(capture=True) as session:
        catalog = discover_vaults(tmp_path, _session=session)
        new = collect_vault_statistics(catalog, count_characters=characters,
                                       include_trash=trash, _session=session)
        assert metrics.counts["reused_directories"] > 0
    assert signature(old) == signature(new)


@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError, OSError])
def test_discovery_unreadable_directory_has_same_issue_and_survivors(tmp_path, reference, monkeypatch, error):
    dataset(tmp_path)
    original = os.scandir
    denied = os.path.normcase(str(tmp_path / "ordinary"))
    def fail(path):
        from obmanage.paths import canonical
        if os.path.normcase(canonical(path)) == denied:
            raise error("injected")
        return original(path)
    monkeypatch.setattr(os, "scandir", fail)
    old = reference["management.catalog"].discover_vaults(tmp_path)
    new = discover_vaults(tmp_path)
    assert signature(old) == signature(new)


def test_discovery_metadata_does_not_enumerate_unrelated_directory_twice(tmp_path):
    dataset(tmp_path)
    metrics = ScanMetrics()
    with metrics.activate(), ScanSession(capture=True) as session:
        catalog = discover_vaults(tmp_path, _session=session)
        before = metrics.counts["directory_enumerations"]
        collect_vault_statistics(catalog, count_characters=False, _session=session)
        after = metrics.counts["directory_enumerations"]
        # All active statistics directories were already observed. Only its real
        # final re-enumeration is added (the marker validation is also reused).
        assert after - before < before


@pytest.mark.skipif(os.name != "nt", reason="Windows case ambiguity contract")
def test_case_ambiguous_enumeration_is_not_a_complete_catalog_or_statistics(tmp_path, monkeypatch):
    from obmanage.paths import canonical
    from obmanage.models import SyncError
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "note.md").write_bytes(b"data")
    original = os.scandir
    with original(root) as iterator:
        entries = list(iterator)
    note = next(e for e in entries if e.name == "note.md")
    class Alias:
        name = "Note.md"
        path = str(root / "Note.md")
        def stat(self, **kwargs): return note.stat(**kwargs)
    class Directory:
        def __enter__(self): return iter([*entries, Alias()])
        def __exit__(self, *_): pass
    def enumerate(path):
        return Directory() if canonical(path) == canonical(root) else original(path)
    monkeypatch.setattr(os, "scandir", enumerate)
    assert any(i.code == "ambiguous_name" for i in discover_vaults(root).issues)
    result = collect_vault_statistics(root, count_characters=False)
    assert not result.statistics[0].complete
    assert any(i.code == "ambiguous_name" for i in result.issues)
    with ScanSession() as session:
        assert not session.manifest(root).complete
    with pytest.raises(SyncError): _scan(str(root), None, None)


def test_long_chinese_paths_keep_full_snapshot_and_discovery_contract(tmp_path, reference):
    from obmanage.paths import native
    root = tmp_path
    for _ in range(8): root = root / ("中文长路径" * 4)
    os.makedirs(native(root / ".obsidian"))
    with open(native(root / "笔记🙂.md"), "wb") as output:
        output.write("中文🙂".encode("utf8"))
    assert len(str(root)) > 260
    assert signature(discover_vaults(tmp_path)) == signature(reference["management.catalog"].discover_vaults(tmp_path))
    with ScanSession() as session:
        assert session.manifest(root).dictionaries() == reference["engine"]._scan(str(root), None, None)


def test_statistics_reports_a_new_vault_outside_previously_discovered_vaults(tmp_path):
    (tmp_path / "known/.obsidian").mkdir(parents=True)
    (tmp_path / "ordinary").mkdir()
    with ScanSession(capture=True) as session:
        catalog = discover_vaults(tmp_path, _session=session)
        (tmp_path / "ordinary/new/.obsidian").mkdir(parents=True)
        result = collect_vault_statistics(catalog, count_characters=False, _session=session)
        assert any(i.code == "discovery_changed" for i in result.issues)
        assert all(not item.complete for item in result.statistics)


def test_discovery_final_check_does_not_count_unrelated_file_edits_as_a_new_vault(tmp_path):
    (tmp_path / "known/.obsidian").mkdir(parents=True)
    (tmp_path / "ordinary").mkdir()
    path = tmp_path / "ordinary/data.bin"
    path.write_bytes(b"old")
    with ScanSession(capture=True) as session:
        catalog = discover_vaults(tmp_path, _session=session)
        path.write_bytes(b"new content")
        result = collect_vault_statistics(catalog, count_characters=False, _session=session)
        assert not result.issues


def test_statistics_rechecks_excluded_nested_marker_after_content_reads(tmp_path, monkeypatch):
    import obmanage.management.statistics as implementation
    root = tmp_path / "parent"
    (root / ".obsidian").mkdir(parents=True)
    marker = root / "child/.obsidian"
    marker.mkdir(parents=True)
    (root / "x.md").write_text("body", encoding="utf8")
    original = implementation._read_utf8_characters
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        marker.rmdir()
        return result
    monkeypatch.setattr(implementation, "_read_utf8_characters", changed)
    result = collect_vault_statistics(root)
    assert any(i.code == "nested_boundary_changed" for i in result.issues)
    assert not result.statistics[0].complete


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_real_windows_junction_is_rejected_without_entering_its_target(tmp_path, reference):
    import _winapi
    from obmanage.models import SyncError
    root, outside = tmp_path / "collection", tmp_path / "outside"
    root.mkdir()
    (outside / ".obsidian").mkdir(parents=True)
    note = outside / "keep.md"
    note.write_bytes(b"outside must remain read-only")
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    old = reference["management.catalog"].discover_vaults(root)
    new = discover_vaults(root)
    assert signature(old) == signature(new)
    assert not new.vaults and any(i.code == "unsafe_item" for i in new.issues)
    with ScanSession() as session:
        result = session.manifest(root)
        assert not result.complete and not any("keep.md" in p for p in result.entries)
        with pytest.raises(SyncError): result.require_complete()
    assert note.read_bytes() == b"outside must remain read-only"


def test_diagnostics_count_bytes_read_even_when_utf8_decoding_fails(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    data = b"not valid utf8: \xff"
    (tmp_path / "invalid.md").write_bytes(data)
    metrics = ScanMetrics()
    with metrics.activate():
        result = collect_vault_statistics(tmp_path)
    assert metrics.counts["markdown_bytes"] == len(data)
    assert not result.statistics[0].complete
