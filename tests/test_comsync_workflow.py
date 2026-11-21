from __future__ import annotations

import shutil
from threading import Event

import pytest

from obmanage.management import comsync_workflow as workflow
from obmanage.management.comsync import components
from obmanage.management.comsync_targets import DirectoryBinding
from obmanage.management.deployment import DeploymentEngine, DeploymentError
from obmanage.models import SyncCancelled


@pytest.fixture
def sample(tmp_path, monkeypatch):
    collection = tmp_path / "collection"
    vaults = {name: collection / name for name in ("Source", "A", "B", "C")}
    for name, vault in vaults.items():
        (vault / ".obsidian").mkdir(parents=True)
        (vault / ".obsidian" / "workspace.json").write_text(name, encoding="utf-8")
        (vault / ".claude").mkdir()
        (vault / ".claude" / "settings.json").write_text(name, encoding="utf-8")
    state = tmp_path / "state"

    def freeze(*names, chosen=("obsidian",), open_after=False):
        return workflow.ComSyncRequest(
            1, str(vaults["Source"]), DirectoryBinding.capture(str(collection)),
            tuple(workflow.ComSyncTarget(name, DirectoryBinding.capture(str(vaults[name])))
                  for name in names),
            chosen, components(str(vaults["Source"]), chosen), "test", open_after,
        )

    def unexpected(*_args, **_kwargs):
        pytest.fail("Unexpected session operation")

    for method in ("close_target_sessions", "request_open_target", "assert_target_sessions_closed"):
        monkeypatch.setattr(workflow, method, unexpected)
    return state, vaults, freeze


def _workspace(vault):
    return vault / ".obsidian" / "workspace.json"


def _run(state, request, confirm, cancel=None):
    return workflow.synchronize(state, None, request, cancel or Event(), None, confirm)


def test_preview_is_read_only_under_same_request(sample):
    state, vaults, freeze = sample
    plan = workflow.preview(state, freeze("B"), Event(), None)
    assert plan.needs_deploy
    assert _workspace(vaults["B"]).read_text() == "B"
    assert DeploymentEngine(state).list_batches() == ()


def test_close_save_and_final_confirmation_apply_only_frozen_write_vaults(sample, monkeypatch):
    state, vaults, freeze = sample
    for relative in (".obsidian/workspace.json", ".claude/settings.json"):
        shutil.copyfile(vaults["Source"] / relative, vaults["C"] / relative)
    b, c = map(str, (vaults["B"], vaults["C"]))
    closes, openings, checks = [], [], []
    live = {str(vaults["A"]), b, c}

    def close(paths, *, on_closed, expected_identities, **_):
        assert paths == (b,)
        assert expected_identities[b] == DirectoryBinding.capture(b).identity
        _workspace(vaults["B"]).write_text("saved on close", encoding="utf-8")
        live.remove(b)
        closes.append(b)
        on_closed(b)

    def confirm(approval):
        assert approval.write_targets == (b,)
        assert approval.closed_by_task == (b,)
        assert approval.skipped == (f"{c} · 本轮预检查一致，已跳过",)
        scanned = next(item for item in approval.plan.targets if item.component_id == "obsidian")
        assert next(entry.snapshot.size for entry in scanned.target_tree.entries if entry.relative_path
                    == "workspace.json") == len("saved on close")
        _workspace(vaults["C"]).write_text("new session save", encoding="utf-8")
        return True

    def check(paths, **_):
        assert paths == (b,)
        assert b not in live
        checks.append(paths)

    def opened(path, **_):
        openings.append(path)
        live.add(path)
        return True

    monkeypatch.setattr(workflow, "close_target_sessions", close)
    monkeypatch.setattr(workflow, "assert_target_sessions_closed", check)
    monkeypatch.setattr(workflow, "request_open_target", opened)
    result = _run(state, freeze("B", "C", chosen=("obsidian", "claude")), confirm)
    assert result.status == "success"
    assert result.execution.committed_targets == 2
    assert closes == openings == [b]
    assert len(checks) >= 4
    assert live == {str(vaults["A"]), b, c}
    assert _workspace(vaults["B"]).read_text() == "Source"
    assert _workspace(vaults["C"]).read_text() == "new session save"
    assert _workspace(vaults["A"]).read_text() == "A"
    assert result.openings[0].restore


@pytest.mark.parametrize("open_after", [True, False])
def test_all_unchanged_skips_closing_confirmation_and_transaction(sample, monkeypatch, open_after):
    state, vaults, freeze = sample
    shutil.copyfile(_workspace(vaults["Source"]), _workspace(vaults["B"]))
    opened = []
    monkeypatch.setattr(workflow, "request_open_target", lambda path, **_: opened.append(path) or True)
    result = _run(state, freeze("B", open_after=open_after),
                  lambda _approval: pytest.fail("No writes to authorize"))
    assert result.status == "no_changes"
    assert result.execution is None
    assert DeploymentEngine(state).list_batches() == ()
    assert opened == ([str(vaults["B"])] if open_after else [])
    assert len(result.openings) == int(open_after)
    if open_after:
        assert result.openings[0].status == "requested" and not result.openings[0].restore


@pytest.mark.parametrize("reason", ["timeout", "cancel", "decline", "source"])
def test_stopped_preparation_restores_only_original_closed_targets(sample, monkeypatch, reason):
    state, vaults, freeze = sample
    b = str(vaults["B"])
    opened = []

    def close(_paths, *, on_closed, cancel, **_):
        on_closed(b)
        if reason == "timeout":
            raise DeploymentError("later window timed out")
        if reason == "cancel":
            cancel.set()
            raise SyncCancelled("user cancelled")
        if reason == "source":
            _workspace(vaults["Source"]).write_text("replaced source content", encoding="utf-8")

    monkeypatch.setattr(workflow, "close_target_sessions", close)
    monkeypatch.setattr(workflow, "request_open_target", lambda path, **_: opened.append(path) or True)
    result = _run(state, freeze("B", "C"), lambda _approval: False if reason == "decline"
                  else pytest.fail("Preparation should have stopped first"))
    assert result.status == ("cancelled" if reason in {"cancel", "decline"} else "failed")
    assert len(result.openings) == 1 and result.openings[0].restore
    assert opened == [b]
    assert DeploymentEngine(state).list_batches() == ()
    assert _workspace(vaults["B"]).read_text() == "B"


@pytest.mark.parametrize("mutate", ["file", "vault"])
def test_confirmation_cannot_authorize_changed_target(sample, monkeypatch, mutate):
    state, vaults, freeze = sample
    b = vaults["B"]
    monkeypatch.setattr(workflow, "close_target_sessions",
                        lambda _paths, on_closed, **_: on_closed(str(b)))
    monkeypatch.setattr(workflow, "assert_target_sessions_closed", lambda *_args, **_kwargs: None)
    opened = []
    monkeypatch.setattr(workflow, "request_open_target", lambda path, **_: opened.append(path) or True)

    def confirm(_approval):
        if mutate == "vault":
            b.rename(b.with_name("moved"))
            (b / ".obsidian").mkdir(parents=True)
        _workspace(b).write_text("new data after confirmation", encoding="utf-8")
        return True

    result = _run(state, freeze("B", open_after=True), confirm)
    assert result.status == "failed"
    assert DeploymentEngine(state).list_batches() == ()
    assert _workspace(b).read_text() == "new data after confirmation"
    assert opened == ([str(b)] if mutate == "file" else [])
    assert result.openings[0].status == ("requested" if mutate == "file" else "held")


@pytest.mark.parametrize("pending", [True, False])
def test_reopening_during_commit_blocks_next_write_and_recovers(sample, monkeypatch, pending):
    state, vaults, freeze = sample
    b = str(vaults["B"])
    calls, opened = [], []
    monkeypatch.setattr(workflow, "close_target_sessions",
                        lambda _paths, on_closed, **_: on_closed(b))
    monkeypatch.setattr(workflow, "request_open_target", lambda path, **_: opened.append(path) or False)

    def check(paths, **_):
        assert paths == (b,)
        calls.append(paths)
        if len(calls) == 5 or (pending and len(calls) > 5):
            raise DeploymentError("B 已重新打开")

    monkeypatch.setattr(workflow, "assert_target_sessions_closed", check)
    result = _run(state, freeze("B", chosen=("obsidian", "claude")), lambda _approval: True)
    assert result.status == "failed"
    assert result.execution.committed_targets == 1
    assert result.execution.journal_status == ("rollback_blocked" if pending else "rolled_back")
    assert result.execution.errors[0] == "B 已重新打开"
    assert result.openings[0].status == ("held" if pending else "already_running")
    assert opened == ([] if pending else [b])
    assert _workspace(vaults["B"]).read_text() == "B"
    assert (vaults["B"] / ".claude/settings.json").read_text() == ("Source" if pending else "B")


def test_open_failure_is_independent_of_successful_data_commit(sample, monkeypatch):
    state, vaults, freeze = sample
    monkeypatch.setattr(workflow, "close_target_sessions", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(workflow, "assert_target_sessions_closed", lambda *_args, **_kwargs: None)

    def unavailable(*_args, **_kwargs):
        raise OSError("URI failure")

    monkeypatch.setattr(workflow, "request_open_target", unavailable)
    result = _run(state, freeze("B", open_after=True), lambda _approval: True)
    assert result.execution.success
    assert result.status == "success"
    assert result.openings[0].status == "failed" and result.openings[0].error == "URI failure"
    assert _workspace(vaults["B"]).read_text() == "Source"
