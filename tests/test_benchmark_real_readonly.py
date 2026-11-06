import builtins
import io
import json
import os
import shutil
import sys
from pathlib import Path
from threading import Event

import pytest

from obmanage.models import SyncError
from obmanage.paths import canonical, native
from tools import benchmark_readonly_paths as boundaries
from tools import benchmark_real_readonly as benchmark
from tools.benchmark_scanning import api, reference_package


@pytest.fixture
def inputs(tmp_path):
    parent = tmp_path / "parent"
    local, portable, scratch = parent / "LOCAL", tmp_path / "PORTABLE", tmp_path / "scratch"
    for root in (local, portable):
        for vault in (root / "示例", root / "示例" / "nested"):
            (vault / ".obsidian").mkdir(parents=True)
            (vault / "private-title.md").write_bytes("中🙂\r\n".encode() * 18000)
    scratch.mkdir()
    return local, portable, scratch


def arguments(inputs, *extras):
    local, portable, scratch = inputs
    return ["--local-vault", str(local), "--portable-vault", str(portable),
            "--output", str(scratch / "report.json"), *extras]


def test_requires_acknowledgement_and_hash_opt_in(inputs):
    with pytest.raises(SystemExit):
        benchmark.parse_args(arguments(inputs))
    with pytest.raises(SystemExit):
        benchmark.parse_args(arguments(inputs, "--acknowledge-read-only-real-data",
                                        "--mode", "incremental-preview"))
    assert not list(inputs[2].iterdir())


def test_unreviewed_reference_does_not_load_code_or_create_state(inputs):
    local, portable, scratch = inputs
    args = benchmark.parse_args(arguments(inputs, "--acknowledge-read-only-real-data",
                                         "--baseline-ref", "678b112"))
    with pytest.raises(boundaries.BoundaryError, match="reviewed"):
        benchmark.run(args, boundaries.Protection(local, portable, scratch), Event())
    assert not list(scratch.iterdir())


@pytest.mark.parametrize("location", ["equal", "parent", "child", "volume", "dotdot", "case_alias"])
def test_report_intersections_are_rejected_before_scratch_creation(inputs, location):
    local, portable, scratch = inputs
    protection = boundaries.Protection(local, portable, scratch)
    path = {
        "equal": local / "report.json", "parent": local.parent / "report.json",
        "child": portable / "示例" / "report.json", "volume": Path(local.anchor) / "report.json",
        "dotdot": scratch / ".." / "unused.json", "case_alias": local.with_name("local") / "report.json",
    }[location]
    with pytest.raises((ValueError, OSError)):
        protection.report_path(path)
    assert not path.exists() and not list(scratch.iterdir())


def test_report_cannot_use_an_unapproved_parent_or_temp_on_input(inputs):
    local, portable, scratch = inputs
    protection = boundaries.Protection(local, portable, scratch)
    outside = scratch.with_name("other-parent")
    outside.mkdir()
    with pytest.raises(boundaries.BoundaryError, match="temporary directory"):
        protection.report_path(outside / "report.json")
    with pytest.raises(boundaries.BoundaryError, match="protected scan scope"):
        boundaries.Protection(local, portable, portable / "示例")
    assert not list(scratch.iterdir()) and not list(outside.iterdir())


def junction(link, target):
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_reparse_aliases_and_external_targets_fail_before_writes(inputs):
    local, portable, scratch = inputs
    output_link = scratch / "alias"
    junction(output_link, local)
    protection = boundaries.Protection(local, portable, scratch)
    with pytest.raises((ValueError, OSError, SyncError)):
        protection.report_path(output_link / "report.json")
    junction(local / "escape", scratch)
    with pytest.raises(boundaries.BoundaryError, match="reparse target"):
        protection.inventory_epoch(Event())
    assert list(scratch.iterdir()) == [output_link]


def test_missing_replaced_and_remapped_inputs_fail_closed(inputs, monkeypatch):
    local, portable, scratch = inputs
    with pytest.raises((ValueError, OSError)):
        boundaries.Protection(local, portable / "absent", scratch)
    protection = boundaries.Protection(local, portable, scratch)
    original = boundaries.volume_identity
    monkeypatch.setattr(boundaries, "volume_identity",
                        lambda path: "other-volume" if Path(path) == portable else original(path))
    with pytest.raises(boundaries.BoundaryError, match="identity changed"):
        protection.verify()
    monkeypatch.setattr(boundaries, "volume_identity", original)
    portable.rename(portable.with_name("moved"))
    portable.mkdir()
    with pytest.raises(boundaries.BoundaryError, match="identity changed"):
        protection.verify()
    assert not list(scratch.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows directory handle sharing")
def test_writable_directory_remains_bound_until_the_handle_closes(inputs):
    _local, _portable, scratch = inputs
    directory = boundaries.Directory.bind(scratch)
    moved = scratch.with_name("moved-scratch")
    with directory.hold(), pytest.raises(OSError):
        scratch.rename(moved)
    scratch.rename(moved)
    assert moved.is_dir()


@pytest.mark.parametrize("mode", ["discovery", "metadata", "strict-utf8", "incremental-preview"])
def test_scan_calls_no_mutation_of_either_input(inputs, monkeypatch, mode):
    local, portable, scratch = inputs
    modules = api()
    protection = boundaries.Protection(local, portable, scratch)
    expected = protection.inventory_epoch(Event())
    seen = []

    def forbid(path):
        if isinstance(path, (str, os.PathLike)):
            physical = Path(canonical(os.path.realpath(native(path))))
            assert all(root != physical and root not in physical.parents for root in (local, portable))
            assert scratch == physical or scratch in physical.parents
            seen.append(physical)

    def spy(function, *indices):
        def call(*args, **kwargs):
            for index in indices:
                forbid(args[index])
            return function(*args, **kwargs)
        return call

    def stream(function):
        def call(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                forbid(file)
            return function(file, mode, *args, **kwargs)
        return call

    def descriptor(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbid(path)
        return original_open(path, flags, *args, **kwargs)

    original_open = os.open
    with monkeypatch.context() as patches:
        patches.setattr(builtins, "open", stream(builtins.open))
        patches.setattr(io, "open", stream(io.open))
        patches.setattr(os, "open", descriptor)
        for name in ("remove", "unlink", "mkdir", "rmdir", "chmod", "utime"):
            patches.setattr(os, name, spy(getattr(os, name), 0))
        for name in ("rename", "replace"):
            patches.setattr(os, name, spy(getattr(os, name), 0, 1))
        patches.setattr(shutil, "rmtree", spy(shutil.rmtree, 0))
        patches.setattr(shutil, "copytree", spy(shutil.copytree, 0, 1))
        patches.setattr(modules["store"].sqlite3, "connect", spy(modules["store"].sqlite3.connect, 0))

        def never_execute(*_args, **_kwargs):
            pytest.fail("Read-only diagnostic reached an execution entry point")

        patches.setattr(modules["engine"].SyncEngine, "execute", never_execute)
        patches.setattr(modules["management.incremental"].IncrementalEngine, "execute", never_execute)
        result = benchmark._scan(modules, mode, (str(local), str(portable)), scratch / "sqlite", Event())
        assert not result.issues
    assert protection.inventory_epoch(Event()) == expected
    assert bool(seen) == (mode == "incremental-preview")
    if seen:
        assert (scratch / "sqlite" / "baselines.sqlite3").is_file()


@pytest.mark.parametrize("mode", ["discovery", "metadata", "strict-utf8", "incremental-preview"])
def test_exact_frozen_reference_equivalence(inputs, mode):
    local, portable, scratch = inputs
    (portable / "示例" / "private-title.md").write_bytes(b"different")
    roots = (str(local), str(portable))
    with reference_package(benchmark.BASELINE, work_dir=scratch) as old:
        before = benchmark._scan(old, mode, roots, scratch / "old-state", Event())
        after = benchmark._scan(api(), mode, roots, scratch / "new-state", Event())
    assert benchmark._shape(before, mode) == benchmark._shape(after, mode)


@pytest.mark.parametrize("mode", ["discovery", "incremental-preview"])
def test_report_is_anonymous_and_never_overwrites(inputs, mode):
    local, portable, scratch = inputs
    if mode == "incremental-preview":
        (portable / "示例" / "private-title.md").write_bytes(b"different")
    args = benchmark.parse_args(arguments(inputs, "--acknowledge-read-only-real-data", "--counts",
                                         "--mode", mode, "--include-full-hash"))
    protection = boundaries.Protection(local, portable, scratch)
    report = benchmark.run(args, protection, Event())
    raw = args.output.read_text()
    assert json.loads(raw) == report
    assert not any(value in raw for value in (str(local), str(portable), "private-title", "nested/", "baselines.sqlite3"))
    assert all(scenario["wall_ms"] for scenario in report["scenarios"].values())
    if mode == "incremental-preview":
        assert all(sample["bytes_read"] > 0 for sample in report["scenarios"][f"{mode}/pair"]["samples"])
        assert all(sample["changed_or_incomplete_pairs"] for sample in
                   report["scenarios"][f"{mode}/pair"]["samples"])
    assert list(scratch.iterdir()) == [args.output]
    with pytest.raises(boundaries.BoundaryError, match="exists"):
        benchmark.run(args, protection, Event())
    assert args.output.read_text() == raw


def test_preflight_changes_and_cancel_do_not_create_files(inputs, monkeypatch):
    local, portable, scratch = inputs
    protection = boundaries.Protection(local, portable, scratch)
    cancel = Event()
    first = protection.inventory_epoch(cancel)
    (portable / "示例" / "new-note.md").write_bytes(b"new")
    assert protection.inventory_epoch(cancel)["portable"] != first["portable"]
    cancel.set()
    with pytest.raises(InterruptedError):
        protection.inventory_epoch(cancel)
    assert not list(scratch.iterdir())


def test_marker_preflight_does_not_create_a_vault_or_state(inputs):
    local, portable, scratch = inputs
    for marker in portable.rglob(".obsidian"):
        marker.rmdir()
    args = benchmark.parse_args(arguments(inputs, "--acknowledge-read-only-real-data"))
    with pytest.raises(boundaries.BoundaryError, match="portable.*no verified"):
        benchmark.run(args, boundaries.Protection(local, portable, scratch), Event())
    assert not list(scratch.iterdir()) and not list(portable.rglob(".obsidian"))


@pytest.mark.parametrize("during", ["local", "portable"])
def test_drift_invalidates_only_the_affected_comparison(inputs, monkeypatch, during):
    local, portable, scratch = inputs
    args = benchmark.parse_args(arguments(inputs, "--acknowledge-read-only-real-data"))

    def compared(_versions, _protection, _scratch, _mode, role, _cancel, _args):
        if role == during:
            (local / "示例" / "new-note.md").write_bytes(b"unrelated change")
        return [dict(status="valid", version=version, total_wall_ms=100, error_count_by_class={})
                for version in ("before", "after")]

    monkeypatch.setattr(benchmark, "_compare", compared)
    report = benchmark.run(args, boundaries.Protection(local, portable, scratch), Event())
    assert report["input_metadata_stable"] == {"local": False, "portable": True}
    local_result = report["scenarios"]["discovery/local"]
    assert local_result["input_metadata_stable"] is (during != "local")
    assert bool(local_result["wall_ms"]) is (during != "local")
    assert report["scenarios"]["discovery/portable"]["wall_ms"]


def test_missing_windows_api_fails_without_running(inputs, monkeypatch, capsys):
    def unsupported():
        raise boundaries.BoundaryError("Windows volume identity unavailable")
    monkeypatch.setattr(benchmark, "system_temp", unsupported)
    assert benchmark.main(arguments(inputs, "--acknowledge-read-only-real-data")) == 2
    assert "Windows volume identity unavailable" in capsys.readouterr().err
    assert not list(inputs[2].iterdir())


def test_picker_dependency_missing_has_explicit_status(inputs, monkeypatch):
    local, portable, scratch = inputs
    protection = boundaries.Protection(local, portable, scratch)
    args = benchmark.parse_args(arguments(inputs, "--acknowledge-read-only-real-data", "--mode", "picker"))
    monkeypatch.setitem(sys.modules, "tools.benchmark_readonly_picker", None)
    rows = benchmark._compare({"after": api()}, protection, protection.temp, "picker", "local", Event(), args)
    assert rows[0]["error_count_by_class"] == {"native_gui_unavailable": 1}
    assert rows[0]["status"] == "invalid" and not list(scratch.iterdir())
