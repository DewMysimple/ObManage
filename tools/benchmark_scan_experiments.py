"""Run isolated pool and nested-manifest experiments against a frozen reference."""
from __future__ import annotations

import argparse
import inspect
import itertools
import platform
import statistics
import sys
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from threading import Lock, get_ident
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from obmanage.scanning.metrics import ScanMetrics
from tools.benchmark_sandbox import sandbox
from tools.benchmark_scanning import (
    BASELINE, memory_sample, reference_package, signature, volume_info, write_report,
)


@contextmanager
def pool_variant(module, workers, metrics=None):
    """Benchmark-only substitution. Keep the original grouping and validations."""
    with ExitStack() as stack:
        executor_class = module.ThreadPoolExecutor
        if metrics is not None:
            used_workers = set()
            worker_lock = Lock()

            class ObservedExecutor(ThreadPoolExecutor):
                def __init__(self, *args, **kwargs):
                    started = time.perf_counter()
                    super().__init__(*args, **kwargs, initializer=lambda: metrics.add("worker_starts"))
                    metrics.add("pools")
                    metrics.elapsed("pool_constructor", time.perf_counter() - started)

                def submit(self, function, /, *args, **kwargs):
                    submitted = time.perf_counter()

                    def run():
                        started = time.perf_counter()
                        with worker_lock:
                            used_workers.add(get_ident())
                        metrics.elapsed("worker_queue_sum", started - submitted)
                        try:
                            return function(*args, **kwargs)
                        finally:
                            metrics.elapsed("worker_run_sum", time.perf_counter() - started)

                    return super().submit(run)

            executor_class = ObservedExecutor
            stack.enter_context(patch.object(module, "ThreadPoolExecutor", executor_class))
        if workers is not None:
            source = inspect.getsource(inspect.unwrap(module.collect_vault_statistics))
            source = source[source.index("def collect_vault_statistics("):]
            before = ('            with ThreadPoolExecutor(\n'
                      '                max_workers=worker_count,\n'
                      '                thread_name_prefix="obmanage-statistics",\n'
                      '            ) as executor:')
            if source.count(before) != 1:
                raise RuntimeError("The frozen statistics reference no longer has the expected pool boundary")
            pool = None

            def shared_pool():
                nonlocal pool
                if pool is None:
                    pool = stack.enter_context(executor_class(max_workers=workers,
                        thread_name_prefix="obmanage-statistics-experiment"))
                return nullcontext(pool)

            source = source.replace(before, "            with _shared_pool() as executor:")
            source = source.replace(
                "min(_MAX_CHARACTER_WORKERS, len(markdown_candidates))", str(workers))
            globals_copy = {**vars(module), "_shared_pool": shared_pool}
            exec(compile(source, "<benchmark shared pool>", "exec"), globals_copy)
            stack.enter_context(patch.object(module, "collect_vault_statistics",
                globals_copy["collect_vault_statistics"]))
        yield
    if metrics is not None:
        metrics.add("distinct_worker_thread_ids", len(used_workers))


def _statistics(modules, root, **kwargs):
    with modules["session"].ScanSession(kwargs.get("cancel"), capture=True) as session:
        catalog = modules["management.catalog"].discover_vaults(root, _session=session)
        return modules["management.statistics"].collect_vault_statistics(
            catalog, _session=session, **kwargs)


def pools(modules, root, repeats, report):
    module = modules["management.statistics"]
    variants = ("per_vault", "shared_1", "shared_2", "shared_4")
    for label, vaults, files, body_bytes in (
            ("small_1k", 24, 40, 960), ("small_10k", 24, 400, 960),
            ("large", 12, 12, 196608)):
        dataset = root / label
        unit = "中🙂a".encode("utf-8")
        body = unit * (body_bytes // len(unit))
        for index in range(vaults):
            vault = dataset / f"vault-{index}"
            (vault / ".obsidian").mkdir(parents=True)
            for number in range(files):
                note = vault / f"group-{number // 10}" / f"note-{number}.md"
                note.parent.mkdir(exist_ok=True)
                note.write_bytes(body)
        report["datasets"][label] = dict(vaults=vaults, files=vaults * files,
                                          note_bytes=len(body), files_per_directory=10)
        expected = None
        for repeat in range(repeats + 2):
            mode = "timing" if repeat < repeats else "counted" if repeat == repeats else "memory"
            offset = repeat % len(variants)
            for variant in variants[offset:] + variants[:offset]:
                workers = None if variant == "per_vault" else int(variant[-1])
                metrics = ScanMetrics(label)
                with memory_sample(mode == "memory", metrics), pool_variant(
                        module, workers, metrics if mode == "counted" else None):
                    started = time.perf_counter()
                    result = _statistics(modules, dataset)
                metrics.seconds["total"] = time.perf_counter() - started
                assert not result.issues
                values = signature(result)
                if expected is None:
                    expected = values
                assert values == expected, (label, variant)
                report["samples"].append(dict(dataset=label, variant=variant, mode=mode,
                                              repeat=repeat, **metrics.report()))
        print(f"{label} complete", flush=True)


def manifests(modules, root, repeats, report):
    package = modules["session"].__package__
    from importlib import import_module

    collect = import_module(f"{package}.collection").collect_collection
    for depth, files in itertools.product((0, 1, 3, 5), (40, 240)):
        label = f"depth_{depth}_files_{files}"
        collection = root / label
        vault = collection / "vault"
        for level in range(depth + 1):
            (vault / ".obsidian").mkdir(parents=True)
            for number in range(files):
                (vault / f"note-{number}.md").write_bytes(b"synthetic")
            vault /= "child"
        report["datasets"][label] = dict(nested_depth=depth, physical_files=(depth + 1) * files)
        for repeat in range(repeats + 1):
            traced = repeat == repeats
            metrics = ScanMetrics(label)
            started = time.perf_counter()
            with memory_sample(traced, metrics), modules["session"].ScanSession(capture=True) as session:
                result = collect(collection, session)
                metrics.seconds["build_views"] = time.perf_counter() - started
                if traced:
                    metrics.add("python_build_peak_bytes", tracemalloc.get_traced_memory()[1])
                started_projection = time.perf_counter()
                projections = [view.dictionaries()[0] for view in result.manifests.values()]
                metrics.seconds["materialize_views"] = time.perf_counter() - started_projection
            assert not result.catalog.issues
            assert len(projections) == depth + 1
            states = [state for view in result.manifests.values() for state in view.entries.values()
                      if state["kind"] == "file"]
            identities = {(state["device"], state["inode"]) for state in states}
            assert len(identities) == (depth + 1) * files
            metrics.counts.update(physical_files=len(identities), view_file_references=len(states),
                                  view_state_objects=len({id(state) for state in states}),
                                  copied_state_dicts=sum(len(view) for view in projections))
            report["samples"].append(dict(dataset=label, mode="memory" if traced else "timing",
                                          repeat=repeat, **metrics.report()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=("pools", "manifests"))
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--test-parent", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    with sandbox(args.test_parent) as root, reference_package(args.baseline_ref) as modules:
        report = dict(reference=args.baseline_ref, experiment=args.experiment, repeats=args.repeats,
                      platform=platform.platform(), python=platform.python_version(),
                      volume=volume_info(root), cache="OS cache uncontrolled; rotating variants",
                      datasets={}, samples=[])
        if args.experiment == "pools":
            pools(modules, root, args.repeats, report)
            report["medians"] = {}
            for label in report["datasets"]:
                report["medians"][label] = {
                    variant: statistics.median(sample["seconds"]["total"]
                        for sample in report["samples"] if sample["dataset"] == label
                        and sample["variant"] == variant and sample["mode"] == "timing")
                    for variant in ("per_vault", "shared_1", "shared_2", "shared_4")
                }
        else:
            manifests(modules, root, args.repeats, report)
    write_report(args.output, report)


if __name__ == "__main__":
    main()
