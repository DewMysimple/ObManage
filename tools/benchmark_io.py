"""Reproducible synthetic I/O benchmark; never accepts a user vault path."""
from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from obmanage.engine import SyncEngine
from obmanage.management import collect_vault_statistics, discover_vaults
from obmanage.management.deployment import (DeploymentComponent, DeploymentEngine,
    DeploymentRequest, DeploymentSelection, DeploymentTarget)
from obmanage.management.trash import TrashCleanupEngine
from obmanage.management.incremental import IncrementalEngine
from obmanage.management.archive import ArchiveEngine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=300)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--large-mib", type=int, default=16,
                        help="Mixed binary/text fixture size for archive and large-copy checks")
    parser.add_argument("--reverse-only", action="store_true",
                        help="Measure three fresh reverse-cache scans without unrelated I/O")
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be positive")
    if args.large_mib < 0:
        parser.error("--large-mib must not be negative")
    if args.reverse_only:
        samples = []
        for _ in range(3):
            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                source, target = base / "a", base / "b"
                source.mkdir()
                for index in range(args.files):
                    (source / f"{index}.md").write_bytes(b"x" * 1024)
                shutil.copytree(source, target)
                engine = SyncEngine(base / "state")
                assert engine.analyze(source, target).can_execute
                start = time.perf_counter()
                plan = engine.analyze(target, source)
                assert plan.can_execute and plan.counts["skip"] == args.files
                samples.append(time.perf_counter() - start)
        payload = {"files": args.files, "reverse_seconds": samples}
        print(json.dumps(payload, indent=2))
        if args.output:
            args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return
    results = {}
    profiler = cProfile.Profile() if args.profile else None

    def measure(name, operation):
        start = time.perf_counter()
        if profiler:
            profiler.enable()
        value = operation()
        if profiler:
            profiler.disable()
        results[name] = round(time.perf_counter() - start, 4)
        return value

    with tempfile.TemporaryDirectory(prefix="obmanage-benchmark-") as temporary:
        base = Path(temporary).resolve()
        source = base / "collection" / "Source"
        for index in range(args.files):
            for folder in ("notes", ".obsidian/plugins/demo", ".trash"):
                path = source / folder / f"group-{index // 50}" / f"item-{index}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ObManage 合成测试数据\n" * 20, encoding="utf-8")
        target = base / "target"
        shutil.copytree(source, target)
        catalog = measure("discover", lambda: discover_vaults(source.parent))
        stats = measure("statistics", lambda: collect_vault_statistics(catalog))
        assert not stats.issues, stats.issues
        for mode in ("mirror", "no_video"):
            engine = SyncEngine(base / ("state-" + mode))
            first = measure(mode + "_first", lambda: engine.analyze(source, target, mode=mode))
            cached = measure(mode + "_cached", lambda: engine.analyze(source, target, mode=mode))
            assert not first.errors and not cached.errors, (first.errors, cached.errors)
            reverse = measure(mode + "_reverse_cached", lambda: engine.analyze(target, source, mode=mode))
            assert not reverse.errors, reverse.errors
            for index in range(args.files):
                (source / "notes" / f"group-{index // 50}" / f"item-{index}.md").write_text(
                    "Changed " + mode + str(index), encoding="utf-8")
            plan = measure(mode + "_preview", lambda: engine.analyze(source, target, mode=mode))
            result = measure(mode + "_execute", lambda: engine.execute(plan))
            assert result.status == "success", result
        local, portable = base / "incremental-local", base / "incremental-portable"
        shutil.copytree(source, local / "Demo")
        shutil.copytree(target, portable / "Demo")
        (local / "Demo" / "added.md").write_text("incremental", encoding="utf-8")
        incremental = IncrementalEngine(base / "incremental-state")
        analysis = measure("incremental_scan", lambda: incremental.analyze(str(local), str(portable)))
        result = measure("incremental_execute", lambda: incremental.execute(
            analysis, {pair.key: "local" for pair in analysis.pairs}))
        assert result.status == "success", result
        targets = [base / f"deploy-{index}" for index in range(3)]
        for root in targets:
            (root / ".obsidian").mkdir(parents=True)
        deploy = DeploymentEngine(base / "deploy-state")
        request = DeploymentRequest(tuple(DeploymentSelection(
            DeploymentComponent.obsidian(source), DeploymentTarget(str(i), str(root)))
            for i, root in enumerate(targets)))
        plan = measure("deploy_preview", lambda: deploy.analyze(request))
        result = measure("deploy_execute", lambda: deploy.execute(plan))
        assert result.success, result
        result = measure("deploy_rollback", lambda: deploy.rollback(plan.batch_id))
        assert result.success, result
        # Include the full archive lifecycle: all content reads, ZIP verification
        # and final source checks. Never measure just the compressor in isolation.
        archive = ArchiveEngine(base / "archive-state")
        output = base / "archives"
        output.mkdir()
        plan = measure("archive_small_preview", lambda: archive.analyze(str(source), str(output)))
        result = measure("archive_small_execute", lambda: archive.execute(plan))
        assert Path(result.output).is_file()
        if args.large_mib:
            import random
            block = random.Random(20260928).randbytes(512 * 1024) + b"ObManage benchmark\n" * 29127
            block = block[:1024 * 1024]
            with (source / "large.bin").open("wb") as stream:
                for _ in range(args.large_mib):
                    stream.write(block)
            engine = SyncEngine(base / "large-state")
            plan = measure("large_copy_preview", lambda: engine.analyze(source, target))
            result = measure("large_copy_execute", lambda: engine.execute(plan))
            assert result.status == "success", result
            for level in (0, 1, 6):
                plan = measure(f"archive_mixed_l{level}_preview", lambda: archive.analyze(
                    str(source), str(output), filename=f"mixed-{level}.zip", level=level))
                result = measure(f"archive_mixed_l{level}_execute", lambda: archive.execute(plan))
                assert Path(result.output).is_file()
        trash = TrashCleanupEngine(base / "trash-state")
        plan = measure("trash_preview", lambda: trash.analyze([source]))
        result = measure("trash_execute", lambda: trash.execute(plan, [source]))
        assert result.status == "success", result
    payload = {"files_per_tree": args.files, "large_mib": args.large_mib, "seconds": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.profile:
        with args.profile.open("w", encoding="utf-8") as output:
            pstats.Stats(profiler, stream=output).sort_stats("cumulative").print_stats(45)


if __name__ == "__main__":
    main()
