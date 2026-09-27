from pathlib import Path
from threading import Event

import pytest

from obmanage.management import running_vaults as runtime
from obmanage.management.models import OpenedVaultCandidates
from obmanage.models import SyncCancelled


def vault(path):
    (path / ".obsidian").mkdir(parents=True)
    return str(path)


def window(title, handle=1):
    return runtime.ObsidianWindow(handle, 123, title, r"C:\Apps\Obsidian.exe")


def test_only_live_windows_match_closed_and_stale_registered_records(tmp_path):
    live = vault(tmp_path / "Live")
    stale = vault(tmp_path / "Stale")
    result = runtime.match_running_vaults([window("note - Live - Obsidian v1.12.7")],
                                         OpenedVaultCandidates((stale, live)))
    assert [v.path for v in result.vaults] == [live]
    assert not result.issues


@pytest.mark.parametrize("names,title", [
    (("A/Notes", "B/Notes"), "doc - Notes - Obsidian v1.9.0"),
    (("A - B", "B"), "doc - A - B - Obsidian"),
])
def test_duplicate_or_suffix_ambiguous_names_are_never_guessed(tmp_path, names, title):
    paths = tuple(vault(tmp_path / name) for name in names)
    result = runtime.match_running_vaults([window(title)], OpenedVaultCandidates(paths))
    assert not result.vaults
    assert result.issues[0].code == "runtime_ambiguous"


def test_names_with_separators_unicode_and_multiple_windows(tmp_path):
    path = vault(tmp_path / "中文 - 配置")
    result = runtime.match_running_vaults([window("a - b - 中文 - 配置 - Obsidian v1.9.0"),
                                          window("中文 - 配置 - Obsidian v1.9.0", 2)],
                                         OpenedVaultCandidates((path,)))
    assert len(result.vaults) == 1


@pytest.mark.parametrize("title", ["Obsidian", "doc - Unknown - Obsidian", "custom window"])
def test_unmapped_windows_leave_explicit_issues(tmp_path, title):
    result = runtime.match_running_vaults([window(title)],
        OpenedVaultCandidates((vault(tmp_path / "Notes"),)))
    assert not result.vaults and result.issues


def test_no_process_does_not_consult_stale_registry(monkeypatch):
    monkeypatch.setattr(runtime, "obsidian_windows", lambda: ())
    monkeypatch.setattr(runtime, "read_registered_vault_candidates", lambda _: pytest.fail("must not use registry"))
    assert runtime.read_running_vaults().issues[0].code == "runtime_not_found"


def test_changed_window_invalidates_identification(tmp_path, monkeypatch):
    path = vault(tmp_path / "Notes")
    observations = iter([(window("Notes - Obsidian"),), ()])
    monkeypatch.setattr(runtime, "obsidian_windows", lambda: next(observations))
    monkeypatch.setattr(runtime, "read_registered_vault_candidates", lambda _: OpenedVaultCandidates((path,)))
    result = runtime.read_running_vaults()
    assert not result.vaults and result.issues[0].code == "runtime_changed"


def test_inaccessible_runtime_and_cancel_are_explicit(monkeypatch):
    def denied():
        raise OSError("access denied")
    monkeypatch.setattr(runtime, "obsidian_windows", denied)
    assert runtime.read_running_vaults().issues[0].code == "runtime_unavailable"
    cancel = Event()
    cancel.set()
    with pytest.raises(SyncCancelled):
        runtime.match_running_vaults([window("Notes - Obsidian")], OpenedVaultCandidates(), cancel=cancel)
