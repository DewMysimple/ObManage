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
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from obmanage.scanning.metrics import ScanMetrics, phase
from obmanage.paths import native, snapshot
from tools.benchmark_sandbox import sandbox, test_parent

BASELINE = "678b112ee1322330e545d6d2c691fb393fa8786f"
SCENARIOS = ("first", "unchanged", "changed", "deep", "statistics_metadata", "statistics_utf8",
             "archive", "cancel", "error", "error_partial", "new_vault", "final_change")
FAULTS = ("error", "error_partial", "new_vault", "final_change")


def volume_info(path):
    info = {"filesystem": "unknown", "drive_type": "unknown", "physical_bus": "unverified"}
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        mount = ctypes.create_unicode_buffer(32768)
        if kernel.GetVolumePathNameW(ctypes.c_wchar_p(str(path)), mount, len(mount)):
            fs = ctypes.create_unicode_buffer(261)
            if kernel.GetVolumeInformationW(mount, None, 0, None, None, None, fs, len(fs)):
                info["filesystem"] = fs.value
            info["drive_type"] = {
                2: "removable", 3: "fixed", 4: "network", 5: "optical", 6: "ramdisk",
            }.get(kernel.GetDriveTypeW(mount), "unknown")
    return info


def peak_working_set():
    """Windows process lifetime peak in bytes, including native allocations."""
    if os.name != "nt":
        return None
    import ctypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in
            ("peak_working_set", "working_set", "peak_paged_pool", "paged_pool",
             "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    values = MemoryCounters()
    values.cb = ctypes.sizeof(values)
    if kernel.K32GetProcessMemoryInfo(ctypes.c_void_p(kernel.GetCurrentProcess()),
                                      ctypes.byref(values), values.cb):
        return values.peak_working_set
    return None


@contextmanager
def memory_sample(enabled, metrics):
    if not enabled:
        yield
        return
    import tracemalloc

    tracemalloc.start()
    try:
        yield
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        metrics.add("python_traced_peak_bytes", peak)
        peak_rss = peak_working_set()
        if peak_rss is not None:
            metrics.add("process_lifetime_peak_working_set_bytes", peak_rss)


def write_report(output, report):
    output = output.absolute()
    ancestor = next(path for path in output.parents if snapshot(path) is not None)
    test_parent(ancestor)
    output.parent.mkdir(parents=True, exist_ok=True)
    parent, _ = test_parent(output.parent)
    with open(native(parent / output.name), "x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


@contextmanager
def reference_package(ref="c56a6885d2f5f4d08d7d548e7955c40dcd560de2"):
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
                    source = archive.extractfile(member).read().decode("utf-8").replace("\r\n", "\n")
                    # Add timers only: the immutable reference still performs its
                    # original enumeration, comparisons and exception handling.
                    if (member.name.endswith("/management/statistics.py")
                            and 'with phase("final_revalidation"):' not in source):
                        start = source.index("        for directory, expected_folder, expected_children in directory_guards:")
                        end = source.index('\n        report(vault_path, "", force=True)', start)
                        block = source[start:end]
                        source = (source[:start] + '        with _reference_phase("final_revalidation"):\n'
                            + "\n".join("    " + line for line in block.splitlines()) + source[end:])
                        source = source.replace("from __future__ import annotations",
                            "from __future__ import annotations\nfrom obmanage.scanning.metrics import phase as _reference_phase")
                    target.write_text(source, encoding="utf-8", newline="")
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
    modules = {name: importlib.import_module(f"{package}.{name}") for name in (
        "engine", "management.incremental", "management.catalog",
        "management.statistics", "management.archive", "reading", "store", "paths")}
    if importlib.util.find_spec(f"{package}.scanning") is not None:
        modules["metrics"] = importlib.import_module(f"{package}.scanning.metrics")
        modules["session"] = importlib.import_module(f"{package}.scanning.session")
    return modules


@contextmanager
def observations(modules, metrics):
    """Count actual Python filesystem queries equally in old and new versions.

    DirEntry.stat requests count even when Windows supplies metadata in its
    enumeration buffer. OS-internal operations are not represented as syscalls.
    """
    original_scan, original_lstat, original_stat = os.scandir, os.lstat, os.stat
    def query(name, function, *args, **kwargs):
        metrics.add("observed_metadata")
        metrics.add(name)
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        except OSError:
            metrics.add(name + "_errors")
            raise
        finally:
            elapsed = time.perf_counter() - started
            metrics.elapsed("observed_metadata", elapsed)
            metrics.elapsed(name, elapsed)

    class Entry:
        def __init__(self, entry): self.entry = entry
        def __getattr__(self, name): return getattr(self.entry, name)
        def stat(self, *args, **kwargs):
            return query("observed_direntry_stat", self.entry.stat, *args, **kwargs)
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
        return query("observed_lstat", original_lstat, *args, **kwargs)
    def stat(*args, **kwargs):
        return query("observed_stat", original_stat, *args, **kwargs)
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
        if "metrics" not in modules:
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
        else:
            incremental = modules["management.incremental"]
            original_phase = incremental.phase
            final = False

            @contextmanager
            def incremental_phase(name, **kwargs):
                nonlocal final
                counters = ("observed_enumerations", "observed_metadata", "observed_snapshots",
                            "observed_chains", "observed_hash_bytes", "observed_manifest_comparisons",
                            "observed_manifest_copies")
                before = {key: metrics.counts[key] for key in counters}
                final = name == "final_revalidation"
                cpu = time.thread_time()
                try:
                    with original_phase(name, **kwargs):
                        yield
                finally:
                    if final:
                        metrics.elapsed("final_thread_cpu", time.thread_time() - cpu)
                        for key in counters:
                            metrics.add(f"final_{key}", metrics.counts[key] - before[key])
                    final = False

            stack.enter_context(patch.object(incremental, "phase", incremental_phase))
            for target, attribute, counter in (
                (incremental, "_require_manifest", "observed_manifest_comparisons"),
                (modules["session"].ManifestView, "dictionaries", "observed_manifest_copies"),
            ):
                original = getattr(target, attribute)

                def manifest(*args, _original=original, _counter=counter, **kwargs):
                    metrics.add(_counter)
                    cpu = time.thread_time() if final else None
                    started = time.perf_counter() if final else None
                    try:
                        return _original(*args, **kwargs)
                    finally:
                        if cpu is not None:
                            metrics.elapsed("final_manifest_cpu", time.thread_time() - cpu)
                            metrics.elapsed("final_manifest_work", time.perf_counter() - started)

                stack.enter_context(patch.object(target, attribute, manifest))

        # Wrap each imported binding of these functions once. These are
        # attribution counters, not additional requests in observed_metadata.
        paths = modules["paths"]
        package = paths.__package__
        for module in tuple(sys.modules.values()):
            if module is None or not getattr(module, "__name__", "").startswith(package + "."):
                continue
            for attribute, name in (("snapshot", "observed_snapshots"),
                                    ("assert_plain_chain", "observed_chains"),
                                    ("_marker_state", "observed_marker_probes")):
                original = getattr(module, attribute, None)
                if original is None:
                    continue

                def recorded(*args, _original=original, _name=name, **kwargs):
                    metrics.add(_name)
                    started = time.perf_counter()
                    try:
                        return _original(*args, **kwargs)
                    finally:
                        metrics.elapsed(_name, time.perf_counter() - started)

                stack.enter_context(patch.object(module, attribute, recorded))
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


def fixture(base, vaults=12, files=40, portable_base=None):
    local, portable = base / "local", (portable_base or base) / "portable"
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
            (root / ".trash").mkdir()
            (root / ".trash" / "discarded.md").write_bytes(b"discarded\n")
    shutil.copytree(local, portable)
    return local, portable


def signature(result):
    if hasattr(result, "pairs"):
        return (result.identical_count, tuple(result.issues), tuple(
            (pair.key, pair.errors, tuple(
                None if plan is None else (tuple(sorted(plan.context.get("source_entries", {}).items())),
                    tuple(sorted(plan.context.get("target_entries", {}).items())),
                    tuple((i.action, i.relative_path, i.size) for i in plan.items), tuple(plan.errors),
                    plan.context.get("validated"))
                for plan in (pair.local_plan, pair.portable_plan))) for pair in result.pairs))
    if hasattr(result, "statistics"):
        from dataclasses import asdict

        return asdict(result)
    return tuple((v.path, v.parent_path) for v in result.vaults), tuple((i.code, i.path) for i in result.issues)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vaults", type=int, default=12)
    parser.add_argument("--files", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=SCENARIOS)
    parser.add_argument("--test-parent", type=Path,
                        help="Existing dedicated test directory. Only a new temporary child is used.")
    parser.add_argument("--timing-only", action="store_true", help="Measure without diagnostic instrumentation.")
    parser.add_argument("--memory", action="store_true", help="Trace Python allocations separately from timing runs.")
    args = parser.parse_args()
    if min(args.vaults, args.files, args.repeats) < 1:
        parser.error("counts must be positive")
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()

    baseline = git("rev-parse", "--verify", "--end-of-options", args.baseline_ref + "^{commit}")
    report = dict(baseline=baseline, candidate_head=git("rev-parse", "HEAD"),
                  candidate_tracked_dirty=bool(git("status", "--porcelain", "-uno")),
                  environment=dict(platform=platform.platform(), python=platform.python_version(),
                  cpu=platform.processor(), diagnostics=not args.timing_only, memory_traced=args.memory,
                  cache="OS cache uncontrolled; alternating versions"),
                  dataset=dict(vaults=args.vaults + 1, files_per_vault=args.files,
                               nested=True, attachment_bytes=262144), samples=[])
    scenarios = [name for name in SCENARIOS if name in args.scenarios]
    if any(s in scenarios for s in ("unchanged", "changed", "deep")) and "first" not in scenarios:
        parser.error("include first when measuring unchanged, changed, or deep")
    external = sandbox(args.test_parent) if args.test_parent is not None else nullcontext(None)
    with reference_package(baseline) as old, sandbox() as base, external as portable_temp:
        local, portable = fixture(base, args.vaults, args.files, portable_temp)
        statistics_root = portable if portable_temp else local
        report["environment"].update(local=volume_info(local), portable=volume_info(portable),
            portable_location="explicit_test_parent" if portable_temp else "system_temp",
            statistics_side="portable" if portable_temp else "local")
        changed = local / "group-0/vault-0/notes/part-0/note-0.md"
        original_body = changed.read_bytes()
        report["dataset"].update(files=sum(p.is_file() for p in local.rglob("*")),
            directories=sum(p.is_dir() for p in local.rglob("*")), mode="mirror/metadata/strict_utf8",
            nested_depth=1, active_markdown_files=(args.vaults + 1) * args.files,
            active_markdown_bytes_per_file=len(original_body),
            video_bytes_per_vault=4096, settings_bytes_per_vault=2, trash_bytes_per_vault=10)
        versions = {"before": old, "after": api()}
        late = local / "new-outer"
        denied_part = "part-1" if args.files > 10 else "part-0"
        for repeat in range(args.repeats):
            for scenario in scenarios:
                results = []
                if scenario in ("changed", "deep"):
                    changed.write_bytes(bytes((65 + repeat % 26,)) + original_body[1:])
                for version in (("before", "after") if repeat % 2 == 0 else ("after", "before")):
                    modules = versions[version]
                    state = base / f"state-{repeat}-{version}"
                    if scenario in FAULTS or scenario == "cancel":
                        state = state / scenario
                    engine = modules["management.incremental"].IncrementalEngine(state)
                    metrics = ScanMetrics(scenario)
                    event = Event()
                    progress_calls = 0
                    cancel_requested = None
                    final_started = False
                    mutated = False
                    moved = local / "group-0/vault-0/notes/part-0/late-rename.md"
                    def progress(update):
                        nonlocal progress_calls, cancel_requested, final_started
                        progress_calls += 1
                        if scenario == "cancel" and progress_calls == 3:
                            cancel_requested = time.perf_counter()
                            event.set()
                        final_started = final_started or "正在复核两端集合" in update.message
                    real_scandir = os.scandir
                    def failing_scandir(path):
                        nonlocal mutated
                        if scenario == "error" and str(path).rstrip("\\/").endswith(denied_part):
                            raise PermissionError("injected benchmark failure")
                        if (scenario == "error_partial"
                                and os.path.normcase(str(path)).endswith(os.path.normcase(
                                    str(Path("local/group-0/vault-0/notes/part-0"))))):
                            raise PermissionError("injected single-vault benchmark failure")
                        if final_started and not mutated and scenario in ("new_vault", "final_change"):
                            mutated = True
                            if scenario == "new_vault":
                                (late / "new-vault" / ".obsidian").mkdir(parents=True)
                            else:
                                changed.rename(moved)
                        return real_scandir(path)
                    own_metrics = (modules["metrics"].ScanMetrics(scenario)
                                   if "metrics" in modules else metrics)
                    active = own_metrics.activate() if not args.timing_only else nullcontext()
                    observed = observations(modules, metrics) if not args.timing_only else nullcontext()
                    injected = (patch.object(os, "scandir", failing_scandir)
                                if scenario in FAULTS else nullcontext())
                    try:
                        with memory_sample(args.memory, metrics), injected, observed, active:
                            started = time.perf_counter()
                            if scenario.startswith("statistics"):
                                if "session" in modules:
                                    with modules["session"].ScanSession(capture=True) as session:
                                        catalog = modules["management.catalog"].discover_vaults(
                                            statistics_root, _session=session)
                                        result = modules["management.statistics"].collect_vault_statistics(
                                            catalog, count_characters=scenario == "statistics_utf8", _session=session)
                                else:
                                    catalog = modules["management.catalog"].discover_vaults(statistics_root)
                                    result = modules["management.statistics"].collect_vault_statistics(
                                        catalog, count_characters=scenario == "statistics_utf8")
                                assert not result.issues, result.issues
                            elif scenario == "archive":
                                result = modules["management.archive"].ArchiveEngine(base / f"archive-state-{version}").analyze(
                                    str(local), str(base), filename="preview.zip", exclude_videos=True)
                            else:
                                result = engine.analyze(str(local), str(portable), deep=scenario == "deep",
                                                        cancel=event, progress=progress)
                                if scenario in FAULTS:
                                    assert result.issues or any(p.errors for p in result.pairs)
                                else:
                                    assert not result.issues and not any(p.errors for p in result.pairs), result
                    except BaseException as exc:
                        if scenario != "cancel" or type(exc).__name__ != "SyncCancelled":
                            raise
                        result = None
                    finished = time.perf_counter()
                    if scenario == "cancel":
                        assert result is None and cancel_requested is not None
                    if own_metrics is not metrics:
                        metrics.counts.update(own_metrics.counts)
                        metrics.seconds.update(own_metrics.seconds)
                    metrics.seconds["total"] = finished - started
                    if cancel_requested is not None:
                        metrics.seconds["cancel_latency"] = finished - cancel_requested
                    metrics.status = ("cancelled" if scenario == "cancel" else "incomplete"
                                      if scenario in FAULTS else "success")
                    if scenario.startswith("statistics") or scenario in ("first", "unchanged", "changed", "deep"):
                        results.append(signature(result))
                    if scenario in ("new_vault", "final_change"):
                        assert mutated, "fault did not run"
                    if late.exists():
                        late.rename(base / f"late-{repeat}-{version}")
                    if moved.exists():
                        moved.rename(changed)
                    sample = dict(version=version, repeat=repeat, scenario=scenario, **metrics.report())
                    sample["slow_vaults"] = own_metrics.report().get("slow_vaults", [])
                    if hasattr(result, "pairs"):
                        sample["result"] = dict(collection_issues=len(result.issues),
                            pair_errors=sum(bool(pair.errors) for pair in result.pairs),
                            validated_plans=sum(bool(plan.context.get("validated"))
                                for pair in result.pairs for plan in (pair.local_plan, pair.portable_plan) if plan))
                    report["samples"].append(sample)
                if results:
                    assert results[0] == results[1], scenario
                if scenario in ("changed", "deep"):
                    changed.write_bytes(original_body)
            print(f"repeat {repeat + 1}/{args.repeats} complete", flush=True)
    report["medians"] = {}
    for scenario in {s["scenario"] for s in report["samples"]}:
        report["medians"][scenario] = {version: statistics.median(
            s["seconds"]["total"] for s in report["samples"] if s["scenario"] == scenario and s["version"] == version)
            for version in ("before", "after")}
    write_report(args.output, report)
    print(json.dumps(report["medians"], indent=2))


if __name__ == "__main__":
    main()
