"""Compare initial copy and its rescans on generated Windows data only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from threading import Event

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.benchmark_readonly_paths import Directory, system_temp
from tools.benchmark_sandbox import sandbox
from tools.benchmark_scanning import api, reference_package
from tools.experiment_initial_sync import audit, inventory, projection, totals


BASELINE = "b77fb441f6cbc4d4347694f5ca4b38e17a368bdd"


def fixture(source, files):
    scopes = tuple(f"vault_{i + 1:03}" for i in range(4))
    for scope in scopes:
        (source / scope / ".obsidian").mkdir(parents=True)
        (source / scope / "empty").mkdir()
    for index in range(files):
        file = source / scopes[index % len(scopes)] / f"dir_{index % 16}" / f"{index:05}.md"
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(hashlib.sha256(str(index).encode()).digest() * 64)
    (source / scopes[-1] / "media.bin").write_bytes(bytes(range(256)) * 32768)
    return scopes


def sample(modules, source, target, state, expected, scopes, *, create_only=False):
    target.mkdir()
    engine = modules["management.incremental"].IncrementalEngine(state)
    metric_type = modules["metrics"].ScanMetrics

    def run(name, action, observe):
        metrics = metric_type(name)
        started = time.perf_counter()
        with metrics.activate() if observe else nullcontext():
            value = action()
        return value, time.perf_counter() - started, metrics.report()

    def analyze(deep=False):
        return engine.analyze(str(source), str(target), deep=deep)

    initial, preview_seconds, _ = run("initial_preview", analyze, False)
    choices = {path: "local" for path in scopes}
    pairs = modules["management.incremental"].selected_plans(initial, choices)
    for pair, plan in pairs:
        relative = pair.relative_path + "/"
        actions = {item.relative_path: (item.action, item.size) for item in plan.items}
        assert actions == {p[len(relative):]: ("add" if s["kind"] == "file" else "mkdir", s["size"])
                           for p, s in expected.items() if p.startswith(relative)}
    kwargs = {"create_only": True} if create_only else {}
    result, execute_seconds, metrics = run("execute", lambda: engine.execute(initial, choices, **kwargs),
                                           create_only)
    assert result.status == "success", result.errors
    assert len(result.outcomes) == len(scopes)
    assert not any(o.result.deleted_files or o.result.deleted_dirs or o.result.renamed_items
                   for o in result.outcomes)
    assert sum(o.result.copied_bytes for o in result.outcomes) == totals(expected)["bytes"]
    verification = {}
    audit(source, target, expected, Event(), verification)
    rescans = {}
    for label, deep in (("warm", False), ("deep", True)):
        outcome, seconds, counts = run(label, lambda: analyze(deep), True)
        assert outcome.identical_count == len(scopes) and not outcome.pairs and not outcome.issues
        rescans[label] = dict(seconds=seconds, counts=counts["counts"])
    return dict(preview_seconds=preview_seconds, execute_seconds=execute_seconds,
                rescans=rescans, audit=verification["audits"][0], execution=metrics if create_only else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--rounds", type=int, default=3, choices=range(1, 6))
    args = parser.parse_args()
    if not 4 <= args.files <= 20000:
        parser.error("provide 4–20000 synthetic small files")
    temp = Directory.bind(system_temp())
    results = {key: [] for key in ("2.8.3", "current", "create_only_counted")}
    with temp.hold(), sandbox(temp.physical) as root, reference_package(BASELINE, work_dir=root) as reference:
        for round_index in range(args.rounds):
            directory = root / str(round_index)
            source = directory / "source"
            scopes = fixture(source, args.files)
            expected = projection(inventory(source, Event(), status="fixture"), scopes)
            current = api()
            variants = (("2.8.3", reference), ("current", current))
            for label, modules in variants if round_index % 2 == 0 else variants[::-1]:
                results[label].append(sample(modules, source, directory / label, directory / (label + "-state"),
                                             expected, scopes))
            results["create_only_counted"].append(sample(current, source, directory / "counted",
                directory / "counted-state", expected, scopes, create_only=True))
        median = {label: {phase: statistics.median(row[phase] for row in rows)
                          for phase in ("preview_seconds", "execute_seconds")}
                  for label, rows in results.items()}
        print(json.dumps(dict(baseline=BASELINE, work=totals(expected), rounds=args.rounds,
                              median=median, samples=results), indent=2))


if __name__ == "__main__":
    main()
