"""Compare frozen and current ObManage read-only scans of two Windows collections."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from threading import Event

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from obmanage.management.catalog import discover_vaults
from obmanage.paths import native
from tools.benchmark_readonly_paths import BoundaryError, Protection, system_temp
from tools.benchmark_sandbox import sandbox
from tools.benchmark_scanning import ROOT, api, memory_sample, observations, reference_package, volume_info

BASELINE = "f1c34ddc01b421ab4fe905af724e932a62df1a10"
MODES = ("discovery", "metadata", "strict-utf8", "incremental-preview", "picker")


def _git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                   stderr=subprocess.DEVNULL).strip()


def _error(exc):
    if isinstance(exc, InterruptedError) or type(exc).__name__ == "SyncCancelled":
        return "cancelled"
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, FileNotFoundError):
        return "unavailable"
    if isinstance(exc, OSError):
        return "io"
    if type(exc).__name__ == "SyncError" or isinstance(exc, BoundaryError):
        return "safety_check"
    if isinstance(exc, (ImportError, AttributeError)):
        return "api_unavailable"
    return "diagnostic_error"


def _shape(result, mode):
    """Exact result in memory, including errors and both directions' contexts."""
    if mode != "incremental-preview":
        return asdict(result)
    shape = asdict(result)
    shape.pop("duration_seconds")
    for pair in shape["pairs"]:
        for side in ("local_plan", "portable_plan"):
            plan = pair[side]
            if plan is not None:
                plan.pop("created_at")
                context = plan["context"]
                if "protected_target_directories" in context:
                    context["protected_target_directories"] = sorted(context["protected_target_directories"])
    return shape


def _summarize(result, mode):
    if mode == "incremental-preview":
        errors = Counter()
        errors["batch_incomplete"] += len(result.issues)
        errors["pair_incomplete"] += sum(len(pair.errors) for pair in result.pairs)
        plans = [plan for pair in result.pairs for plan in (pair.local_plan, pair.portable_plan) if plan]
        totals = Counter(item.action for plan in plans for item in plan.items)
        scope = dict(vault_count=None if result.issues else result.identical_count + len(result.pairs),
                     actions=dict(totals),
                     changed_or_incomplete_pairs=len(result.pairs), identical_pairs=result.identical_count,
                     validated_directions=sum(bool(plan.context.get("validated")) for plan in plans))
    elif mode == "discovery" or mode == "picker":
        errors = Counter(issue.code for issue in result.issues)
        scope = dict(vault_count=len(result.vaults), nested_vaults=sum(v.is_nested for v in result.vaults))
        if not result.vaults:
            errors["no_vault_marker"] += 1
    else:
        errors = Counter(issue.code for issue in result.issues)
        errors["incomplete_vault"] += sum(not row.complete for row in result.statistics)
        scope = dict(vault_count=len(result.statistics), file_count=sum(r.total_files for r in result.statistics),
                     file_bytes=sum(r.total_bytes for r in result.statistics),
                     markdown_files=sum(r.markdown_files for r in result.statistics),
                     directory_count=sum(r.folders for r in result.statistics))
        if not result.statistics:
            errors["no_vault_marker"] += 1
    return scope, dict(+errors)


def _scan(modules, mode, roots, state, cancel, *, deep=False):
    catalog = modules["management.catalog"].discover_vaults
    if mode == "discovery":
        return catalog(roots[0], cancel=cancel)
    if mode == "incremental-preview":
        engine = modules["management.incremental"].IncrementalEngine(state)
        return engine.analyze(*roots, deep=deep, cancel=cancel)
    with modules["session"].ScanSession(cancel, capture=True) as session:
        vaults = catalog(roots[0], cancel=cancel, _session=session)
        with modules["metrics"].phase("statistics"):
            return modules["management.statistics"].collect_vault_statistics(
                vaults, count_characters=mode == "strict-utf8", cancel=cancel, _session=session)


def _measure(modules, mode, roots, state, cancel, args):
    metrics = modules["metrics"].ScanMetrics(mode)
    detail = args.counts or args.memory
    started = time.perf_counter()
    stage = "scan"
    try:
        with (
            observations(modules, metrics) if args.counts else nullcontext(),
            metrics.activate() if detail else nullcontext(),
            memory_sample(args.memory, metrics),
        ):
            started = time.perf_counter()
            result = _scan(modules, mode, roots, state, cancel, deep=args.deep)
            wall = (time.perf_counter() - started) * 1000
        stage = "result_comparison"
        scale, errors = _summarize(result, mode)
        # Hash locally to compare the full stable output without retaining a
        # second copy of both versions' potentially large plans between rounds.
        encoded = json.dumps(_shape(result, mode), sort_keys=True, ensure_ascii=True).encode("ascii")
        digest = hashlib.sha256(encoded).digest()
    except Exception as exc:
        wall = (time.perf_counter() - started) * 1000
        scale = {"failure_stage": stage, "failure_type": type(exc).__name__}
        errors, digest = {_error(exc): 1}, None
    row = dict(status="invalid" if errors else "valid", total_wall_ms=wall,
               error_count_by_class=errors, **scale)
    if detail:
        details = metrics.report()
        counts = details["counts"]
        row["counts"] = counts
        row["phase_ms"] = {key: seconds * 1000 for key, seconds in details["seconds"].items()}
        row["bytes_read"] = counts.get("hash_bytes", 0) + counts.get("observed_markdown_bytes",
                                                                 counts.get("markdown_bytes", 0))
        row["files_hashed"] = counts.get("hash_files", 0)
        row["slow_vaults"] = details.get("slow_vaults", [])
    return row, digest


def _compare(versions, protection, scratch, mode, role, cancel, args):
    roots = tuple(str(protection.inputs[key].physical)
                  for key in (protection.inputs if role == "pair" else (role,)))
    rows = []
    expected = None
    for repeat in range(args.repeats):
        order = list(versions) if repeat % 2 == 0 else list(reversed(versions))
        for version in order:
            if cancel.is_set():
                return rows
            print(f"  {version} {repeat + 1}/{args.repeats}", file=sys.stderr, flush=True)
            protection.verify()
            scratch.verify()
            state = scratch.physical / f"sqlite-{mode}-{role}-{version}-{repeat}"
            if mode == "picker":
                try:
                    from tools.benchmark_readonly_picker import measure_picker

                    row, digest = measure_picker(versions[version], roots[0], scratch.physical, cancel, args)
                except ImportError:
                    row, digest = dict(status="invalid", error_count_by_class={"native_gui_unavailable": 1}), None
            else:
                row, digest = _measure(versions[version], mode, roots, state, cancel, args)
            protection.verify()
            if expected is None:
                expected = digest
            row.update(version=version, repeat=repeat + 1, equivalent=digest == expected and digest is not None)
            rows.append(row)
            if row["status"] != "valid" or not row["equivalent"]:
                if row["status"] == "valid":
                    row["error_count_by_class"]["result_not_equivalent"] = 1
                for sample in rows:
                    sample["status"] = "invalid"
                return rows
    return rows


def _medians(rows):
    result = {}
    for version in ("before", "after"):
        valid = [r for r in rows if r["version"] == version and r["status"] == "valid"]
        if valid:
            result[version] = {
                key: dict(median=statistics.median(values), min=min(values), max=max(values), n=len(values))
                for key in ("total_wall_ms", "first_visible_ms", "verified_ready_ms", "selection_accepted_ms")
                if (values := [r[key] for r in valid if key in r])
            }
    return result


def run(args, protection, cancel):
    parent, output = protection.report_path(args.output)
    baseline = _git("rev-parse", "--verify", "--end-of-options", args.baseline_ref + "^{commit}")
    if baseline != BASELINE:
        raise BoundaryError("This tool accepts only its reviewed 2.8.2 baseline commit")
    for role, root in protection.inputs.items():
        if not discover_vaults(root.physical, cancel=cancel).vaults:
            raise BoundaryError(f"{role} scan scope has no verified .obsidian vault marker")
    epoch = protection.inventory_epoch(cancel)
    report = dict(baseline=baseline, candidate_head=_git("rev-parse", "HEAD"),
                  candidate_tracked_dirty=bool(_git("status", "--porcelain", "-uno")),
                  python=sys.version.split()[0], windows_build=sys.getwindowsversion().build if os.name == "nt" else None,
                  sample_kind="counts" if args.counts else "memory" if args.memory else "wall",
                  cache="OS cache uncontrolled; AB/BA alternating order", deep=args.deep,
                  picker_registry="isolated empty registry, real collection only",
                  volumes={key: volume_info(root.physical) for key, root in protection.inputs.items()}, scenarios={})
    with protection.temp.hold(), sandbox(protection.temp.physical, validate=protection.outside) as scratch:
        scratch_directory = protection.outside(scratch)
        with scratch_directory.hold(), reference_package(baseline, work_dir=scratch) as reference:
            versions = {"before": reference, "after": api()}
            for mode in args.mode:
                for role in (("pair",) if mode == "incremental-preview" else ("local", "portable")):
                    print(f"{mode} / {role}", file=sys.stderr, flush=True)
                    roles = tuple(protection.inputs) if role == "pair" else (role,)
                    before = protection.inventory_epoch(cancel, roles)
                    rows = _compare(versions, protection, scratch_directory, mode, role, cancel, args)
                    stable = before == protection.inventory_epoch(cancel, roles)
                    if not stable:
                        for sample in rows:
                            sample["status"] = "invalid"
                            sample["error_count_by_class"]["input_metadata_changed"] = 1
                    report["scenarios"][f"{mode}/{role}"] = dict(
                        samples=rows, input_metadata_stable=stable, wall_ms=_medians(rows))
        if cancel.is_set():
            raise InterruptedError
        end_epoch = protection.inventory_epoch(cancel)
        report["input_metadata_stable"] = {role: epoch[role] == end_epoch[role] for role in epoch}
        with parent.hold():
            protection.report_path(output)
            with open(native(output), "x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=True, indent=2)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("local-vault", "portable-vault", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--mode", nargs="+", choices=MODES, default=["discovery"])
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--baseline-ref", default=BASELINE, help="Git ref resolving to the reviewed 2.8.2 commit.")
    parser.add_argument("--acknowledge-read-only-real-data", action="store_true")
    parser.add_argument("--include-full-hash", action="store_true",
                        help="Permit incremental preview to read file contents with a fresh isolated baseline.")
    parser.add_argument("--deep", action="store_true", help="Use complete content validation in incremental preview.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--counts", action="store_true", help="Collect detailed counts separately from wall timing.")
    group.add_argument("--memory", action="store_true", help="Trace memory separately from wall timing.")
    args = parser.parse_args(argv)
    if not args.acknowledge_read_only_real_data:
        parser.error("--acknowledge-read-only-real-data is required")
    if "incremental-preview" in args.mode and not args.include_full_hash:
        parser.error("Even fast preview may hash with an empty baseline. Pass --include-full-hash to allow it.")
    if args.deep and "incremental-preview" not in args.mode:
        parser.error("--deep requires incremental-preview")
    args.mode = tuple(dict.fromkeys(args.mode))
    return args


def main(argv=None):
    args = parse_args(argv)
    cancel = Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        protection = Protection(args.local_vault, args.portable_vault, system_temp())
        result = run(args, protection, cancel)
        return int(any(not scenario["wall_ms"] for scenario in result["scenarios"].values()))
    except BoundaryError as exc:
        print(f"Safety preflight stopped: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Read-only diagnosis stopped: {_error(exc)}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
