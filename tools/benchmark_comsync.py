"""Compare .comSync previews on synthetic temporary vaults only.

Example: python tools/benchmark_comsync.py --targets 46 --files 100
         --repeats 3 --baseline-ref HEAD --report artifacts/comsync-preview.json
No source or target repository paths are accepted. Analysis never executes a
deployment, closes Obsidian, or opens a vault.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from obmanage.management import deployment
from obmanage.management.comsync import components


def _load_baseline(ref: str, directory: Path) -> tuple[ModuleType, str]:
    """Load committed deployment code with its relative project imports intact."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
        cwd=PROJECT_ROOT, text=True, encoding="utf-8", stderr=subprocess.PIPE,
    ).strip()
    source = subprocess.check_output(
        ["git", "show", revision + ":obmanage/management/deployment.py"],
        cwd=PROJECT_ROOT, stderr=subprocess.PIPE,
    )
    path = directory / "baseline_deployment.py"
    path.write_bytes(source)
    name = "obmanage.management._benchmark_baseline_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the baseline deployment module")
    module = importlib.util.module_from_spec(spec)
    # Dataclass creation looks up its defining module during execution.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module, revision


def _fixture(directory: Path, targets: int, files: int) -> tuple[Path, tuple[Path, ...]]:
    source = directory / "source"
    folders = (".obsidian", ".claude", ".claudian", "File/Templater")
    for folder in folders:
        for index in range(files):
            path = source / folder / f"group-{index // 25}" / f"item-{index:05}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 1024)
    rows = []
    for index in range(targets):
        target = directory / f"target-{index:03}"
        shutil.copytree(source, target)
        for folder in folders:
            (target / folder / "group-0/item-00000.json").write_bytes(b"y" * 1024)
            (target / folder / "old.txt").write_bytes(b"old")
        rows.append(target)
    return source, tuple(rows)


def _request(module: ModuleType, source: Path, targets: tuple[Path, ...]):
    # The baseline has distinct dataclass types and validates isinstance().
    selected = tuple(module.DeploymentComponent(**dataclasses.asdict(component))
                     for component in components(str(source),
                                                 ("obsidian", "claude", "claudian", "file")))
    return module.DeploymentRequest(tuple(
        module.DeploymentSelection(component, module.DeploymentTarget(str(index), str(target)))
        for component in selected for index, target in enumerate(targets)
    ), label="comsync")


def _preview_digest(plan) -> str:
    """Compare every displayed change and full source/target content manifest."""
    payload = [(
        item.component_id, item.target_root, item.target_path,
        item.needs_deploy, item.ensure_directory,
        [dataclasses.asdict(change) for change in item.changes],
        item.source_tree.manifest(),
        item.target_tree.manifest() if item.target_tree is not None else None,
    ) for item in plan.targets]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _measure(module: ModuleType, request, state: Path):
    engine = module.DeploymentEngine(state)
    original_hash = module._hash_file
    hash_reads = 0

    def counted_hash(*args, **kwargs):
        nonlocal hash_reads
        hash_reads += 1
        return original_hash(*args, **kwargs)

    module._hash_file = counted_hash
    try:
        start = time.perf_counter()
        plan = engine.analyze(request)
        elapsed = time.perf_counter() - start
    finally:
        module._hash_file = original_hash
    return plan, elapsed, hash_reads


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=int, default=8)
    parser.add_argument("--files", type=int, default=25,
                        help="Number of 1 KiB files in each of four content components")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1,
                        help="Previews per variant recorded separately and excluded from medians")
    parser.add_argument("--baseline-ref",
                        help="Optional committed Git revision for an alternating comparison")
    parser.add_argument("--report", type=Path, help="Optional JSON result file")
    args = parser.parse_args()
    if min(args.targets, args.files, args.repeats) < 1:
        parser.error("--targets, --files and --repeats must be positive")
    if args.warmups < 0:
        parser.error("--warmups must not be negative")

    samples = {"current": []}
    warmups = {"current": []}
    hash_reads = {"current": []}
    modules = {"current": deployment}
    revision = None
    expected_digest = None
    # Each source component is hashed once, and every target component is read
    # in full, including its extra deletion candidate. Directory-only intents
    # do not read their source or target contents.
    expected_reads = 4 * (args.files * (args.targets + 1) + args.targets)
    with tempfile.TemporaryDirectory(prefix="obmanage-comsync-benchmark-") as temporary:
        base = Path(temporary)
        baseline = None
        try:
            if args.baseline_ref:
                try:
                    baseline, revision = _load_baseline(args.baseline_ref, base)
                except (OSError, subprocess.CalledProcessError) as exc:
                    parser.error(f"Cannot read baseline revision {args.baseline_ref!r}: {exc}")
                modules = {"baseline": baseline, **modules}
                samples["baseline"], hash_reads["baseline"] = [], []
                warmups["baseline"] = []
            source, targets = _fixture(base / "fixture", args.targets, args.files)
            requests = {label: _request(module, source, targets)
                        for label, module in modules.items()}
            for stage, count in (("warmup", args.warmups), ("sample", args.repeats)):
                for repeat in range(count):
                    order = tuple(modules)
                    if repeat % 2:
                        order = tuple(reversed(order))
                    for label in order:
                        plan, elapsed, reads = _measure(modules[label], requests[label],
                                                        base / ("state-" + label))
                        digest = _preview_digest(plan)
                        if expected_digest is None:
                            expected_digest = digest
                        if digest != expected_digest:
                            raise RuntimeError("The complete preview differs between benchmark runs")
                        if reads != expected_reads:
                            raise RuntimeError(f"Expected {expected_reads} full file reads, got {reads}")
                        if stage == "warmup":
                            warmups[label].append(elapsed)
                        else:
                            samples[label].append(elapsed)
                            hash_reads[label].append(reads)
                        print(json.dumps({"stage": stage, "repeat": repeat + 1,
                                          "variant": label, "seconds": round(elapsed, 4),
                                          "hash_reads": reads}), flush=True)
        finally:
            if baseline is not None:
                sys.modules.pop(baseline.__name__, None)

    medians = {label: statistics.median(values) for label, values in samples.items()}
    report = {
        "targets": args.targets, "content_components": 4, "directory_intents": 2,
        "files_per_component": args.files, "bytes_per_file": 1024,
        "repeats": args.repeats, "warmups": args.warmups, "warmup_seconds": warmups,
        "samples_seconds": samples, "median_seconds": medians,
        "hash_reads": hash_reads, "identical_previews": True,
        "baseline_ref": args.baseline_ref, "baseline_commit": revision,
    }
    if "baseline" in medians:
        report["speedup"] = medians["baseline"] / medians["current"]
        report["reduction_percent"] = 100 * (1 - medians["current"] / medians["baseline"])
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.report:
        args.report.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
