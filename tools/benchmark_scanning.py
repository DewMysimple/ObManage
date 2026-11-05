"""Real-disk multi-vault comparison. All data stays in an isolated temporary tree."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import io
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from obmanage.scanning.metrics import ScanMetrics, phase
from obmanage.scanning.session import ScanSession

BASELINE = "c56a6885d2f5f4d08d7d548e7955c40dcd560de2"


@contextmanager
def reference_package(ref=BASELINE):
    """Load immutable Git source under another package name, never change checkout."""
    commit = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                                     cwd=ROOT, text=True).strip()
    raw = subprocess.check_output(["git", "archive", commit, "obmanage"], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="obmanage-reference-") as temp:
        destination = Path(temp) / "scan_reference"
        destination.mkdir()
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if member.isfile() and member.name.endswith(".py"):
                    if parts[0] != "obmanage" or ".." in parts:
                        raise ValueError("Unsafe reference archive member")
                    target = destination.joinpath(*parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member).read().decode("utf-8")
                    # Add timers only: the immutable reference still performs its
                    # original enumeration, comparisons and exception handling.
                    if member.name.endswith("/management/statistics.py"):
                        start = source.index("        for directory, expected_folder, expected_children in directory_guards:")
                        end = source.index('\n        report(vault_path, "", force=True)', start)
                        block = source[start:end]
                        source = (source[:start] + '        with _reference_phase("final_revalidation"):\n'
                            + "\n".join("    " + line for line in block.splitlines()) + source[end:])
                        source = source.replace("from __future__ import annotations",
                            "from __future__ import annotations\nfrom obmanage.scanning.metrics import phase as _reference_phase")
                    target.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "scan_reference", destination / "__init__.py",
            submodule_search_locations=[str(destination)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        try:
            yield api("scan_reference")
        finally:
            for name in list(sys.modules):
                if name == "scan_reference" or name.startswith("scan_reference."):
                    del sys.modules[name]


def api(package="obmanage"):
    return {name: importlib.import_module(f"{package}.{name}") for name in (
        "engine", "management.incremental", "management.catalog",
        "management.statistics", "management.archive", "reading", "store")}


@contextmanager
def observations(modules, metrics):
    """Count actual Python filesystem queries equally in old and new versions.

    DirEntry.stat requests count even when Windows supplies metadata in its
    enumeration buffer. OS-internal operations are not represented as syscalls.
    """
    original_scan, original_lstat, original_stat = os.scandir, os.lstat, os.stat
    class Entry:
        def __init__(self, entry): self.entry = entry
        def __getattr__(self, name): return getattr(self.entry, name)
        def stat(self, *args, **kwargs):
            metrics.add("observed_metadata")
            started = time.perf_counter()
            try:
                return self.entry.stat(*args, **kwargs)
            finally:
                metrics.elapsed("observed_metadata", time.perf_counter() - started)
    class Directory:
        def __init__(self, path):
            metrics.add("observed_enumerations")
            self.iterator = original_scan(path)
        def __enter__(self): return self
        def __exit__(self, *args): return self.iterator.__exit__(*args)
        def __iter__(self): return self
        def __next__(self):
            entry = next(self.iterator)
            metrics.add("observed_entries")
            return Entry(entry)
        def close(self): self.iterator.close()
    def lstat(*args, **kwargs):
        metrics.add("observed_metadata")
        started = time.perf_counter()
        try:
            return original_lstat(*args, **kwargs)
        finally:
            metrics.elapsed("observed_metadata", time.perf_counter() - started)
    def stat(*args, **kwargs):
        metrics.add("observed_metadata")
        started = time.perf_counter()
        try:
            return original_stat(*args, **kwargs)
        finally:
            metrics.elapsed("observed_metadata", time.perf_counter() - started)
    stats_module = modules["management.statistics"]
    original_read = stats_module._read_utf8_characters
    def read(*args, **kwargs):
        callback = kwargs["on_bytes"]
        def record(amount):
            metrics.add("observed_markdown_bytes", amount)
            callback(amount)
        kwargs["on_bytes"] = record
        started = time.perf_counter()
        try:
            return original_read(*args, **kwargs)
        finally:
            metrics.elapsed("observed_utf8_read", time.perf_counter() - started)
    from contextlib import ExitStack
    with ExitStack() as stack:
        for target, attribute, replacement in ((os, "scandir", Directory), (os, "lstat", lstat),
                (os, "stat", stat), (stats_module, "_read_utf8_characters", read)):
            stack.enter_context(patch.object(target, attribute, replacement))
        # Instrument the immutable reference's phase boundaries, without changing logic.
        if modules["engine"].__name__.startswith("scan_reference"):
            from obmanage.scanning.metrics import measured
            discover = measured("discovery")(modules["management.catalog"].discover_vaults)
            stack.enter_context(patch.object(modules["management.catalog"], "discover_vaults", discover))
            stack.enter_context(patch.object(modules["management.incremental"], "discover_vaults", discover))
            real_scan = modules["engine"]._scan
            scan_number = 0
            def scan(*args, **kwargs):
                nonlocal scan_number
                label = "initial_inventory" if scan_number % 4 < 2 else "final_revalidation"
                scan_number += 1
                with phase(label):
                    return real_scan(*args, **kwargs)
            stack.enter_context(patch.object(modules["engine"], "_scan", scan))
            for name, label in (("__init__", "sqlite_open"), ("records", "sqlite_read"),
                                ("save", "sqlite_write"), ("invalidate", "sqlite_write")):
                cls = modules["store"].BaselineStore
                stack.enter_context(patch.object(cls, name, measured(label)(getattr(cls, name))))
        original_open = modules["store"].BaselineStore.__init__
        def opened(*args, **kwargs):
            metrics.add("observed_sqlite_connections")
            return original_open(*args, **kwargs)
        stack.enter_context(patch.object(modules["store"].BaselineStore, "__init__", opened))
        # The hash stream stays the version's own implementation; record real output bytes.
        for module in modules.values():
            if hasattr(module, "hash_stream"):
                original_hash = module.hash_stream
                def hashed(*args, _function=original_hash, **kwargs):
                    started = time.perf_counter()
                    result = _function(*args, **kwargs)
                    metrics.add("observed_hash_files")
                    metrics.add("observed_hash_bytes", result[0])
                    metrics.elapsed("observed_hash", time.perf_counter() - started)
                    return result
                stack.enter_context(patch.object(module, "hash_stream", hashed))
        yield


def fixture(base, vaults=12, files=40):
    local, portable = base / "local", base / "portable"
    for index in range(vaults):
        vault = local / f"group-{index % 3}" / f"vault-{index}"
        for nested in ("", "nested/child") if index == 0 else ("",):
            root = vault / nested
            (root / ".obsidian").mkdir(parents=True)
            (root / ".obsidian" / "app.json").write_bytes(b"{}")
            for number in range(files):
                path = root / "notes" / f"part-{number // 10}" / f"note-{number}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("shared scan 中文🙂\n" * 40, encoding="utf-8")
            (root / "attachment.bin").write_bytes(b"a" * (256 * 1024))
            (root / "clip.mp4").write_bytes(b"v" * 4096)
            (root / "empty").mkdir()
    shutil.copytree(local, portable)
    return local, portable


def signature(result):
    if hasattr(result, "pairs"):
        return (result.identical_count, tuple(result.issues), tuple(
            (pair.key, pair.errors, tuple(
                None if plan is None else (tuple(sorted(plan.context.get("source_entries", {}).items())),
                    tuple(sorted(plan.context.get("target_entries", {}).items())),
                    tuple((i.action, i.relative_path, i.size) for i in plan.items), tuple(plan.errors))
                for plan in (pair.local_plan, pair.portable_plan))) for pair in result.pairs))
    if hasattr(result, "statistics"):
        return (tuple((s.vault_path, s.total_files, s.total_bytes, s.markdown_files,
                       s.utf8_characters, s.folders, s.complete,
                       tuple((t.category, t.files, t.total_bytes, t.extensions) for t in s.file_types))
                      for s in result.statistics), tuple((i.code, i.path) for i in result.issues))
    return tuple((v.path, v.parent_path) for v in result.vaults), tuple((i.code, i.path) for i in result.issues)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vaults", type=int, default=12)
    parser.add_argument("--files", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.vaults, args.files, args.repeats) < 1:
        parser.error("counts must be positive")
    report = dict(environment=dict(platform=platform.platform(), python=platform.python_version(),
                  cache="OS cache uncontrolled; alternating versions, no USB claim"),
                  dataset=dict(vaults=args.vaults + 1, files_per_vault=args.files,
                               nested=True, attachment_bytes=262144), samples=[])
    with reference_package(args.baseline_ref) as old, tempfile.TemporaryDirectory(prefix="obmanage-scan-") as temporary:
        base = Path(temporary)
        local, portable = fixture(base, args.vaults, args.files)
        report["dataset"].update(files=sum(p.is_file() for p in local.rglob("*")),
            directories=sum(p.is_dir() for p in local.rglob("*")), mode="mirror/metadata/strict_utf8")
        versions = {"before": old, "after": api()}
        for repeat in range(args.repeats):
            results = {}
            for version in (("before", "after") if repeat % 2 == 0 else ("after", "before")):
                modules = versions[version]
                engine = modules["management.incremental"].IncrementalEngine(base / f"state-{repeat}-{version}")
                for scenario in ("first", "unchanged", "changed", "statistics_metadata", "statistics_utf8", "archive", "cancel", "error"):
                    changed = local / "group-0/vault-0/notes/part-0/note-0.md"
                    if scenario == "changed":
                        changed.write_text((f"modified-{repeat}" + "x" * 960)[:len(changed.read_bytes())], encoding="utf-8")
                    metrics = ScanMetrics(scenario)
                    event = Event()
                    progress_calls = 0
                    def progress(update):
                        nonlocal progress_calls
                        progress_calls += 1
                        if scenario == "cancel" and progress_calls == 3:
                            event.set()
                    real_scandir = os.scandir
                    def failing_scandir(path):
                        if scenario == "error" and str(path).rstrip("\\/").endswith("part-1"):
                            raise PermissionError("injected benchmark failure")
                        return real_scandir(path)
                    try:
                        with metrics.activate(), patch.object(os, "scandir", failing_scandir), observations(modules, metrics):
                            if scenario.startswith("statistics"):
                                if version == "after":
                                    with ScanSession(capture=True) as session:
                                        catalog = modules["management.catalog"].discover_vaults(local, _session=session)
                                        result = modules["management.statistics"].collect_vault_statistics(
                                            catalog, count_characters=scenario == "statistics_utf8", _session=session)
                                else:
                                    catalog = modules["management.catalog"].discover_vaults(local)
                                    result = modules["management.statistics"].collect_vault_statistics(
                                        catalog, count_characters=scenario == "statistics_utf8")
                                assert not result.issues, result.issues
                            elif scenario == "archive":
                                result = modules["management.archive"].ArchiveEngine(base / f"archive-state-{version}").analyze(
                                    str(local), str(base), filename="preview.zip", exclude_videos=True)
                            else:
                                result = engine.analyze(str(local), str(portable), cancel=event, progress=progress)
                                if scenario == "error":
                                    assert result.issues or any(p.errors for p in result.pairs)
                                else:
                                    assert not result.issues and not any(p.errors for p in result.pairs), result
                    except BaseException as exc:
                        if scenario != "cancel" or type(exc).__name__ != "SyncCancelled":
                            raise
                        result = None
                    if scenario == "error": metrics.status = "incomplete"
                    if scenario == "cancel": assert metrics.status == "cancelled"
                    # Equal length changes produce identical operation shapes; content is compared above.
                    results.setdefault(scenario, {})[version] = (signature(result) if scenario not in ("archive", "cancel") else None)
                    report["samples"].append(dict(version=version, repeat=repeat, scenario=scenario, **metrics.report()))
                    if scenario == "statistics_utf8":
                        changed.write_text("shared scan 中文🙂\n" * 40, encoding="utf-8")
            for scenario, values in results.items():
                # Rewriting the one mutation changes timestamps; compare content-independent statistics
                # and matching pair/action paths separately in the dedicated equivalence tests.
                if scenario.startswith("statistics"):
                    assert values["before"] == values["after"], scenario
            print(f"repeat {repeat + 1}/{args.repeats} complete", flush=True)
    report["medians"] = {}
    for scenario in {s["scenario"] for s in report["samples"]}:
        report["medians"][scenario] = {version: statistics.median(
            s["seconds"]["total"] for s in report["samples"] if s["scenario"] == scenario and s["version"] == version)
            for version in ("before", "after")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["medians"], indent=2))


if __name__ == "__main__":
    main()
