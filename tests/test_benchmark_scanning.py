from pathlib import Path
import os
import subprocess
import json
import sys

import pytest

from obmanage.models import SyncError
from tools.benchmark_sandbox import MARKER, sandbox, test_parent as validate_parent
from tools.benchmark_scanning import write_report


def test_benchmark_creates_exclusive_child_without_touching_parent_contents(tmp_path):
    sentinel = tmp_path / "existing.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with sandbox(tmp_path) as outer, sandbox(tmp_path) as inner:
        assert outer != inner and outer.parent == inner.parent == tmp_path.resolve()
        assert (outer / MARKER).read_bytes() != (inner / MARKER).read_bytes()
        (outer / "vault" / ".obsidian").mkdir(parents=True)
        (inner / "file").touch()
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("marker", (".obsidian", ".trash", ".obmanage-deploy-fake", MARKER))
def test_benchmark_rejects_vaults_and_dangerous_ancestors(tmp_path, marker):
    blocked = tmp_path / "blocked"
    candidate = blocked / "child"
    candidate.mkdir(parents=True)
    if marker in (".obsidian", MARKER):
        (blocked / marker).touch()
        path = candidate
    else:
        path = candidate / marker / "descendant"
        path.mkdir(parents=True)
    with pytest.raises(ValueError):
        with sandbox(path):
            pytest.fail("unsafe parent accepted")
    with pytest.raises(ValueError):
        validate_parent(Path(tmp_path.anchor))


def link_directory(link, target):
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_benchmark_refuses_junction_parent_and_cleanup_keeps_link_target(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    untouched = target / "keep"
    untouched.write_bytes(b"other data")
    junction = tmp_path / "link"
    link_directory(junction, target)
    with pytest.raises(SyncError):
        validate_parent(junction)
    with sandbox(tmp_path) as child:
        link_directory(child / "inserted", target)
    assert untouched.read_bytes() == b"other data"


@pytest.mark.parametrize("change", ("missing_marker", "wrong_marker", "replacement_marker",
                                    "replace_child", "replace_parent", "junction"))
def test_cleanup_refuses_changed_identity_or_marker(tmp_path, change):
    parent = tmp_path / "parent"
    parent.mkdir()
    moved = tmp_path / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    untouched = outside / "keep"
    untouched.touch()
    with pytest.raises(RuntimeError, match="cleanup stopped"):
        with sandbox(parent) as child:
            marker = child / MARKER
            if change == "missing_marker":
                marker.unlink()
            elif change == "wrong_marker":
                marker.write_bytes(b"other run")
            elif change == "replacement_marker":
                content = marker.read_bytes()
                marker.rename(child / "old-marker")
                marker.write_bytes(content)
            elif change == "replace_parent":
                parent.rename(moved)
                parent.mkdir()
                child.mkdir()
            else:
                child.rename(moved)
                if change == "junction":
                    link_directory(child, outside)
                else:
                    child.mkdir()
                    (child / MARKER).write_bytes((moved / MARKER).read_bytes())
    assert child.exists() and untouched.exists()


def test_report_writes_only_new_files_outside_vaults(tmp_path):
    path = tmp_path / "reports" / "new.json"
    write_report(path, {"rows": 1})
    expected = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_report(path, {"rows": 2})
    assert path.read_bytes() == expected
    (tmp_path / "vault" / ".obsidian").mkdir(parents=True)
    with pytest.raises(ValueError):
        write_report(tmp_path / "vault" / "new.json", {})


def test_explicit_test_parent_runs_real_fixture_and_keeps_existing_files(tmp_path):
    test_volume = tmp_path / "dedicated"
    test_volume.mkdir()
    sentinel = test_volume / "existing"
    sentinel.write_bytes(b"preserve")
    output = tmp_path / "results.json"
    command = [sys.executable, "tools/benchmark_scanning.py", "--test-parent", str(test_volume),
               "--vaults", "2", "--files", "3", "--repeats", "1", "--scenarios",
               "first", "statistics_utf8", "cancel", "--output", str(output)]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["environment"]["statistics_side"] == "portable"
    assert report["environment"]["portable_location"] == "explicit_test_parent"
    assert len(report["samples"]) == 6
    for sample in report["samples"]:
        if sample["scenario"] == "statistics_utf8":
            data = report["dataset"]
            assert sample["counts"]["observed_markdown_bytes"] == (
                data["active_markdown_files"] * data["active_markdown_bytes_per_file"])
    assert str(tmp_path) not in output.read_text(encoding="utf-8")
    assert list(test_volume.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve"
