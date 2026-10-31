import pytest

from obmanage.management.deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentError,
    DeploymentRequest,
    DeploymentSelection,
    DeploymentTarget,
)


def make_plan(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.json").write_bytes(b"new")
    roots = tuple(tmp_path / name for name in ("target-a", "target-b"))
    for index, root in enumerate(roots):
        (root / "Config").mkdir(parents=True)
        (root / "Config" / "config.json").write_bytes(f"old-{index}".encode())
    engine = DeploymentEngine(tmp_path / "state")
    component = DeploymentComponent.direct("config", source, "Config")
    request = DeploymentRequest(tuple(
        DeploymentSelection(component, DeploymentTarget(str(index), str(root)))
        for index, root in enumerate(roots)
    ))
    return engine, engine.analyze(request), roots


@pytest.mark.parametrize("reject_at", (1, 2, 3, 4))
def test_safety_check_stops_before_writes_or_restores_committed_targets(tmp_path, reject_at):
    engine, plan, roots = make_plan(tmp_path)
    calls = []

    def safety_check():
        calls.append(tuple((root / "Config" / "config.json").read_bytes() for root in roots))
        if len(calls) == reject_at:
            raise DeploymentError("Obsidian restarted")

    result = engine.execute(plan, safety_check=safety_check)

    assert not result.success
    assert "Obsidian restarted" in result.errors
    assert len(calls) == reject_at
    assert tuple((root / "Config" / "config.json").read_bytes() for root in roots) == (b"old-0", b"old-1")
    if reject_at == 1:
        assert not (tmp_path / "state").exists()
        assert not tuple(tmp_path.glob(".obmanage-deploy-*"))
        assert result.prepared_targets == result.committed_targets == 0
    else:
        assert result.prepared_targets == 2
        assert result.journal_status in {"prepare_failed", "rolled_back"}
    if reject_at == 4:
        assert calls[-1] == (b"new", b"old-1")
        assert result.committed_targets == 1
        assert result.rolled_back_targets == 2
    else:
        assert result.committed_targets == 0


def test_safety_check_runs_at_all_commit_barriers(tmp_path):
    engine, plan, roots = make_plan(tmp_path)
    observed = []

    def safety_check():
        observed.append(tuple((root / "Config" / "config.json").read_bytes() for root in roots))

    result = engine.execute(plan, safety_check=safety_check)

    assert result.success
    assert observed == [(b"old-0", b"old-1")] * 3 + [(b"new", b"old-1")]
    assert tuple((root / "Config" / "config.json").read_bytes() for root in roots) == (b"new", b"new")
