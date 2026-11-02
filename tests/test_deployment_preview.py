"""Batch preview boundaries, independent of path-comparison implementation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from obmanage.management.deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentError,
    DeploymentRequest,
    DeploymentSelection,
    DeploymentTarget,
)


def put(root: Path, relative: str, content: bytes = b"source") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def selections(source: Path, targets: tuple[tuple[Path, str], ...]) -> DeploymentRequest:
    return DeploymentRequest(tuple(
        DeploymentSelection(
            DeploymentComponent.direct(f"component-{index}", source, destination),
            DeploymentTarget(f"target-{index}", str(root)),
        )
        for index, (root, destination) in enumerate(targets)
    ))


def test_batch_preview_accepts_sibling_names_with_shared_prefix(tmp_path):
    source, target = tmp_path / "source", tmp_path / "source-neighbor"
    put(source, "item.txt")
    target.mkdir()
    state = tmp_path / "state"

    plan = DeploymentEngine(state).analyze(selections(source, (
        (target, "Config"), (target, "Configuration"), (target, "Config-other"),
    )))

    assert len(plan.targets) == 3
    assert all(item.needs_deploy for item in plan.targets)
    assert all(item.source_tree is plan.targets[0].source_tree for item in plan.targets)
    assert list(target.iterdir()) == []
    assert not state.exists()


@pytest.mark.parametrize("destinations", [
    ("Config", "Unrelated", "Config/Child"),
    ("Config/Child", "Unrelated", "Config"),
    ("Config", "Unrelated", "Config"),
    ("Config", "Unrelated", "CONFIG"),
])
def test_batch_preview_rejects_nonadjacent_nested_or_equivalent_targets(
        tmp_path, destinations):
    source, target = tmp_path / "source", tmp_path / "target"
    put(source, "item.txt")
    target.mkdir()
    state = tmp_path / "state"

    with pytest.raises(DeploymentError, match="多个限定目标不能相同或互相包含"):
        DeploymentEngine(state).analyze(selections(source, tuple(
            (target, destination) for destination in destinations
        )))

    assert list(target.iterdir()) == []
    assert not state.exists()


def test_batch_preview_rejects_target_within_source_authorization_root(tmp_path):
    source_vault = tmp_path / "source-vault"
    source_file = put(source_vault, ".obsidian/app.json")
    nested_target = source_vault / "nested-vault"
    nested_target.mkdir()
    outside_target = tmp_path / "outside-target"
    outside_target.mkdir()
    component = DeploymentComponent.subtree(
        "config", source_vault, ".obsidian", destination=".claude"
    )
    request = DeploymentRequest(tuple(
        DeploymentSelection(component, DeploymentTarget(str(index), str(root)))
        for index, root in enumerate((outside_target, nested_target))
    ))
    state = tmp_path / "state"

    with pytest.raises(DeploymentError, match="组件来源与任一限定目标不能相同或互相包含"):
        DeploymentEngine(state).analyze(request)

    assert source_file.read_bytes() == b"source"
    assert list(nested_target.iterdir()) == list(outside_target.iterdir()) == []
    assert not state.exists()


def test_batch_preview_rejects_target_subtree_containing_the_source(tmp_path):
    target = tmp_path / "target"
    source = target / "Config" / "Original"
    source_file = put(source, "item.txt")
    state = tmp_path / "state"

    with pytest.raises(DeploymentError, match="组件来源与任一限定目标不能相同或互相包含"):
        DeploymentEngine(state).analyze(selections(source, ((target, "Config"),)))

    assert source_file.read_bytes() == b"source"
    assert list((target / "Config").iterdir()) == [source]
    assert not state.exists()


def test_each_batch_preview_reads_content_despite_equal_sizes_and_timestamps(tmp_path):
    source = tmp_path / "source"
    first, second = tmp_path / "first", tmp_path / "second"
    source_file = put(source, "item.txt", b"AAAA")
    first_file = put(first, "Config/item.txt", b"AAAA")
    second_file = put(second, "Config/item.txt", b"BBBB")
    timestamp = source_file.stat().st_mtime_ns
    for path in (source_file, first_file, second_file):
        os.utime(path, ns=(timestamp, timestamp))
    state = tmp_path / "state"
    engine = DeploymentEngine(state)
    request = selections(source, ((first, "Config"), (second, "Config")))

    before = engine.analyze(request)
    assert [item.changes[0].action for item in before.targets] == ["skip", "update"]

    source_file.write_bytes(b"BBBB")
    os.utime(source_file, ns=(timestamp, timestamp))
    assert {(path.stat().st_size, path.stat().st_mtime_ns)
            for path in (source_file, first_file, second_file)} == {(4, timestamp)}
    after = engine.analyze(request)

    assert [item.changes[0].action for item in after.targets] == ["update", "skip"]
    assert first_file.read_bytes() == b"AAAA"
    assert second_file.read_bytes() == b"BBBB"
    assert not state.exists()
