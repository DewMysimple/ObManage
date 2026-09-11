from __future__ import annotations

import builtins
import json
import os
import shutil
import stat
import threading
from pathlib import Path

import pytest

import obmanage.management.deployment as deployment
import obmanage.management.journal as deployment_journal
from obmanage.management.deployment import (
    DeploymentComponent,
    DeploymentEngine,
    DeploymentError,
    DeploymentRequest,
    DeploymentSelection,
    DeploymentTarget,
)


def put(root: Path, relative: str, content: bytes) -> Path:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def overwrite_journal(state: Path, batch_id: str, change) -> None:
    path = state / "deployment-journal" / f"{batch_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def request_for(source: Path, targets: list[tuple[str, Path]],
                destination: str = "Config") -> DeploymentRequest:
    component = DeploymentComponent.direct("config", source, destination)
    return DeploymentRequest(tuple(
        DeploymentSelection(component, DeploymentTarget(target_id, str(root)))
        for target_id, root in targets
    ), label="test deployment")


@pytest.fixture
def layout(tmp_path: Path):
    source = tmp_path / "source"
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    state = tmp_path / "state"
    source.mkdir()
    vault_a.mkdir()
    vault_b.mkdir()
    return source, vault_a, vault_b, state


def test_preview_is_file_level_and_analysis_does_not_write_targets(layout):
    source, vault_a, _, state = layout
    put(source, "same.txt", b"same")
    put(source, "changed.txt", b"new")
    put(source, "added.txt", b"add")
    target = vault_a / "Config"
    put(target, "same.txt", b"same")
    put(target, "changed.txt", b"old")
    put(target, "removed.txt", b"remove")
    before = tree_bytes(vault_a)

    plan = DeploymentEngine(state).analyze(request_for(source, [("a", vault_a)]))

    assert tree_bytes(vault_a) == before
    assert not state.exists()
    changes = {(item.action, item.relative_path) for item in plan.targets[0].changes}
    assert changes == {
        ("skip", "same.txt"),
        ("update", "changed.txt"),
        ("add", "added.txt"),
        ("delete", "removed.txt"),
    }
    assert plan.targets[0].counts == {"add": 1, "update": 1, "delete": 1, "skip": 1}


def test_successful_deployment_can_be_rolled_back_from_new_instance(layout):
    source, vault_a, _, state = layout
    put(source, "nested/new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))

    result = engine.execute(plan)

    assert result.success
    assert result.batch_id == plan.batch_id
    assert tree_bytes(vault_a / "Config") == {"nested/new.txt": b"new"}
    stored = DeploymentEngine(state).get_batch(plan.batch_id)
    assert stored.status == "committed"
    assert stored.targets[0].backup_path
    assert Path(stored.targets[0].backup_path).exists()

    rollback = DeploymentEngine(state).rollback(plan.batch_id)

    assert rollback.success
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"old"}
    assert DeploymentEngine(state).get_batch(plan.batch_id).status == "rolled_back"


def test_finalize_from_new_instance_keeps_deployment_and_removes_only_backup(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    plan = DeploymentEngine(state).analyze(request_for(source, [("a", vault_a)]))
    assert DeploymentEngine(state).execute(plan).success
    stored = DeploymentEngine(state).get_batch(plan.batch_id)
    backup = Path(stored.targets[0].backup_path or "")

    result = DeploymentEngine(state).finalize(plan.batch_id)

    assert result.success
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}
    assert not backup.exists()
    assert DeploymentEngine(state).get_batch(plan.batch_id).status == "finalized"


@pytest.mark.parametrize("retrying", [False, True])
def test_finalize_refuses_missing_live_deployment_and_preserves_backup(layout, retrying):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    stored = engine.get_batch(plan.batch_id)
    target_record = stored.targets[0]
    backup = Path(target_record.backup_path or "")
    if retrying:
        engine.journal.set_batch(plan.batch_id, status="finalizing")
        engine.journal.set_target(
            plan.batch_id, target_record.selection_id, phase="finalizing"
        )
    displaced = source.parent / f"displaced-before-finalize-{retrying}"
    (vault_a / "Config").rename(displaced)

    result = DeploymentEngine(state).finalize(plan.batch_id)

    assert result.status == "failed"
    assert result.journal_status == "finalize_required"
    assert backup.exists()
    assert tree_bytes(backup) == {"old.txt": b"old"}
    assert tree_bytes(displaced) == {"new.txt": b"new"}
    assert not (vault_a / "Config").exists()


def test_identical_target_is_skipped_without_inode_or_backup_change(layout):
    source, vault_a, _, state = layout
    put(source, "same.txt", b"same")
    target = vault_a / "Config"
    put(target, "same.txt", b"same")
    before = target.stat().st_ino, (target / "same.txt").stat().st_ino
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))

    result = engine.execute(plan)

    assert result.success
    assert not plan.targets[0].needs_deploy
    assert plan.targets[0].counts["skip"] == 1
    assert before == (target.stat().st_ino, (target / "same.txt").stat().st_ino)
    stored = engine.get_batch(plan.batch_id)
    assert stored.targets[0].phase == "unchanged"
    assert stored.targets[0].backup_path is None


@pytest.mark.parametrize("stale_side", ["source", "target"])
def test_stale_source_or_target_fails_before_any_target_write(layout, stale_side):
    source, vault_a, _, state = layout
    put(source, "value.txt", b"new")
    target = vault_a / "Config"
    put(target, "value.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    changed = source / "value.txt" if stale_side == "source" else target / "value.txt"
    changed.write_bytes(b"changed after preview")
    before = tree_bytes(target)

    result = engine.execute(plan)

    assert result.status == "failed"
    assert tree_bytes(target) == before
    assert not state.exists()


def test_second_target_prepare_failure_commits_zero_targets(layout, monkeypatch):
    source, vault_a, vault_b, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"a")
    put(vault_b / "Config", "old.txt", b"b")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a), ("b", vault_b)]))
    original = engine._copy_to_stage

    def fail_second(plan_value, item, stage, cancel, progress):
        if item.target_id == "b":
            put(Path(stage), "new.txt", b"partial")
            raise DeploymentError("injected prepare failure")
        return original(plan_value, item, stage, cancel, progress)

    monkeypatch.setattr(engine, "_copy_to_stage", fail_second)
    result = engine.execute(plan)

    assert result.status == "failed"
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"a"}
    assert tree_bytes(vault_b / "Config") == {"old.txt": b"b"}
    assert not tuple(vault_a.glob(".obmanage-deploy-*"))
    assert not tuple(vault_b.glob(".obmanage-deploy-*"))
    assert engine.get_batch(plan.batch_id).status == "prepare_failed"


def test_commit_failure_stops_and_rolls_back_all_touched_targets(layout, monkeypatch):
    source, vault_a, vault_b, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"a")
    put(vault_b / "Config", "old.txt", b"b")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a), ("b", vault_b)]))
    original_rename = deployment._rename_directory

    def fail_second_install(source_path: str, target_path: str):
        if source_path.endswith(".stage") and Path(target_path) == vault_b / "Config":
            raise OSError("injected activation failure")
        original_rename(source_path, target_path)

    monkeypatch.setattr(deployment, "_rename_directory", fail_second_install)
    result = engine.execute(plan)

    assert result.status == "failed"
    assert result.journal_status == "rolled_back"
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"a"}
    assert tree_bytes(vault_b / "Config") == {"old.txt": b"b"}
    assert result.rolled_back_targets == 2


def test_multiple_targets_share_exactly_one_frozen_source_snapshot(layout):
    source, vault_a, vault_b, state = layout
    put(source, "new.txt", b"new")
    plan = DeploymentEngine(state).analyze(
        request_for(source, [("a", vault_a), ("b", vault_b)])
    )

    assert plan.targets[0].source_tree is plan.targets[1].source_tree
    assert plan.targets[0].source_tree.root_snapshot == plan.targets[1].source_tree.root_snapshot


def test_templater_resolver_accepts_vault_file_or_component_and_creates_missing_parent(tmp_path):
    template_vault = tmp_path / "template"
    source = template_vault / "File" / "Templater"
    put(source, "template.md", b"template")
    target_vault = tmp_path / "target"
    target_vault.mkdir()
    state = tmp_path / "state"

    for provided in (template_vault, template_vault / "File", source):
        component = DeploymentComponent.templater(provided)
        request = DeploymentRequest((DeploymentSelection(
            component, DeploymentTarget("target", str(target_vault))
        ),))
        plan = DeploymentEngine(state).analyze(request)
        assert plan.targets[0].source_path == str(source.resolve())
        # Only execute once; subsequent resolver cases correctly preview it as equal.
        if provided == template_vault:
            assert DeploymentEngine(state).execute(plan).success
            assert (target_vault / "File" / "Templater" / "template.md").read_bytes() == b"template"
            assert DeploymentEngine(state).finalize(plan.batch_id).success


def test_obsidian_resolver_accepts_vault_or_obsidian_directory(tmp_path):
    template = tmp_path / "template"
    put(template / ".obsidian", "app.json", b"{}")
    target = tmp_path / "target"
    target.mkdir()
    state = tmp_path / "state"
    for source in (template, template / ".obsidian"):
        request = DeploymentRequest((DeploymentSelection(
            DeploymentComponent.obsidian(source), DeploymentTarget("target", str(target))
        ),))
        plan = DeploymentEngine(state).analyze(request)
        assert plan.targets[0].source_path == str((template / ".obsidian").resolve())


def test_one_request_can_deploy_multiple_components_to_same_selected_vault(tmp_path):
    suite = tmp_path / "suite"
    put(suite / ".obsidian", "app.json", b"settings")
    put(suite / "File" / "Templater", "note.md", b"template")
    target = tmp_path / "target"
    target.mkdir()
    state = tmp_path / "state"
    target_spec = DeploymentTarget("target", str(target))
    request = DeploymentRequest((
        DeploymentSelection(DeploymentComponent.obsidian(suite), target_spec),
        DeploymentSelection(DeploymentComponent.templater(suite), target_spec),
    ))

    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    result = engine.execute(plan)

    assert result.success
    assert (target / ".obsidian" / "app.json").read_bytes() == b"settings"
    assert (target / "File" / "Templater" / "note.md").read_bytes() == b"template"
    assert engine.finalize(plan.batch_id).success


def test_link_in_source_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = put(tmp_path, "outside.txt", b"outside")
    try:
        os.symlink(outside, source / "linked.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(DeploymentError, match="链接|重解析点|特殊"):
        DeploymentEngine(tmp_path / "state").analyze(request_for(source, [("t", target)]))


def test_link_in_existing_target_is_rejected(tmp_path):
    source = tmp_path / "source"
    put(source, "plain.txt", b"plain")
    outside = put(tmp_path, "outside.txt", b"outside")
    target = tmp_path / "target"
    (target / "Config").mkdir(parents=True)
    try:
        os.symlink(outside, target / "Config" / "linked.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(DeploymentError, match="链接|重解析点|特殊"):
        DeploymentEngine(tmp_path / "state").analyze(request_for(source, [("t", target)]))


def test_source_and_target_same_or_contained_are_rejected_before_execution(tmp_path):
    target = tmp_path / "vault"
    source = target / "Config"
    put(source, "value.txt", b"value")

    with pytest.raises(DeploymentError, match="相同|包含"):
        DeploymentEngine(tmp_path / "state").analyze(request_for(source, [("t", target)]))


def test_rollback_refuses_to_remove_deployment_when_backup_is_not_verified(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    put(Path(record.backup_path or ""), "old.txt", b"corrupt backup")
    deployed_before = tree_bytes(vault_a / "Config")

    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.status == "failed"
    assert tree_bytes(vault_a / "Config") == deployed_before
    assert Path(record.backup_path or "").exists()


def test_finalize_refuses_changed_backup_and_never_touches_deployed_target(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    put(Path(record.backup_path or ""), "unexpected.txt", b"do not delete")

    result = DeploymentEngine(state).finalize(plan.batch_id)

    assert result.status == "failed"
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}
    assert Path(record.backup_path or "").exists()


def test_finalize_cleans_verified_windows_readonly_backup(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    backup_file = Path(record.backup_path or "") / "old.txt"
    os.chmod(backup_file, stat.S_IREAD)

    result = DeploymentEngine(state).finalize(plan.batch_id)

    assert result.success
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}
    assert not Path(record.backup_path or "").exists()


def test_rollback_cleans_verified_windows_readonly_deployment(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    os.chmod(vault_a / "Config" / "new.txt", stat.S_IREAD)

    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.success
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"old"}


def test_cancel_during_preparation_commits_zero_targets(layout, monkeypatch):
    source, vault_a, vault_b, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"a")
    put(vault_b / "Config", "old.txt", b"b")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a), ("b", vault_b)]))
    cancel = threading.Event()
    original = engine._copy_to_stage

    def cancel_after_first(plan_value, item, stage, event, progress):
        result = original(plan_value, item, stage, event, progress)
        if item.target_id == "a":
            cancel.set()
        return result

    monkeypatch.setattr(engine, "_copy_to_stage", cancel_after_first)
    result = engine.execute(plan, cancel)

    assert result.status == "cancelled"
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"a"}
    assert tree_bytes(vault_b / "Config") == {"old.txt": b"b"}


def test_every_filesystem_write_stays_outside_source(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "nested/new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    source_key = os.path.normcase(str(source.resolve()))

    def assert_not_source(path_value):
        value = deployment.canonical(path_value)
        value = os.path.normcase(os.path.normpath(value))
        assert os.path.normcase(os.path.commonpath((source_key, value))) != source_key

    original_open = builtins.open
    original_mkdir = deployment.os.mkdir
    original_unlink = deployment.os.unlink
    original_rmdir = deployment.os.rmdir
    original_rename = deployment.os.rename
    original_utime = deployment.os.utime

    def guarded_open(path_value, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            assert_not_source(path_value)
        return original_open(path_value, mode, *args, **kwargs)

    def guarded_one(original):
        def call(path_value, *args, **kwargs):
            assert_not_source(path_value)
            return original(path_value, *args, **kwargs)
        return call

    def guarded_rename(source_value, target_value, *args, **kwargs):
        assert_not_source(source_value)
        assert_not_source(target_value)
        return original_rename(source_value, target_value, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(deployment.os, "mkdir", guarded_one(original_mkdir))
    monkeypatch.setattr(deployment.os, "unlink", guarded_one(original_unlink))
    monkeypatch.setattr(deployment.os, "rmdir", guarded_one(original_rmdir))
    monkeypatch.setattr(deployment.os, "rename", guarded_rename)
    monkeypatch.setattr(deployment.os, "utime", guarded_one(original_utime))
    before = tree_bytes(source)

    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    assert tree_bytes(source) == before


@pytest.mark.parametrize("operation", ["rollback", "finalize"])
def test_tampered_journal_target_cannot_escape_root_or_touch_canary(layout, monkeypatch, operation):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    canary = layout[0].parent / f"outside-{operation}"
    put(canary, "canary.txt", b"never touched")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    backup = Path(record.backup_path or "")
    overwrite_journal(
        state, plan.batch_id,
        lambda value: value["targets"][0].update(target_path=str(canary)),
    )
    canary_key = os.path.normcase(str(canary.resolve()))

    def forbid_canary(path_value):
        if isinstance(path_value, int):
            return
        path = deployment.canonical(path_value)
        key = os.path.normcase(os.path.normpath(path))
        try:
            assert os.path.normcase(os.path.commonpath((canary_key, key))) != canary_key
        except ValueError:
            return

    original_open = builtins.open
    original_scandir = os.scandir
    original_lstat = os.lstat
    original_rename = os.rename
    original_unlink = os.unlink
    original_rmdir = os.rmdir
    original_chmod = os.chmod

    def guarded_open(path_value, *args, **kwargs):
        forbid_canary(path_value)
        return original_open(path_value, *args, **kwargs)

    def guard_one(original):
        def call(path_value, *args, **kwargs):
            forbid_canary(path_value)
            return original(path_value, *args, **kwargs)
        return call

    def guarded_rename(source_value, target_value, *args, **kwargs):
        forbid_canary(source_value)
        forbid_canary(target_value)
        return original_rename(source_value, target_value, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "scandir", guard_one(original_scandir))
    monkeypatch.setattr(os, "lstat", guard_one(original_lstat))
    monkeypatch.setattr(os, "rename", guarded_rename)
    monkeypatch.setattr(os, "unlink", guard_one(original_unlink))
    monkeypatch.setattr(os, "rmdir", guard_one(original_rmdir))
    monkeypatch.setattr(os, "chmod", guard_one(original_chmod))

    result = getattr(DeploymentEngine(state), operation)(plan.batch_id)

    assert result.status == "failed"
    assert original_open(canary / "canary.txt", "rb").read() == b"never touched"
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}
    assert backup.exists()


@pytest.mark.parametrize("bad_relative", ["../outside", "./Config", "C:escape", "File//Templater"])
def test_journal_rejects_non_bounded_relative_target(layout, bad_relative):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    overwrite_journal(
        state, plan.batch_id,
        lambda value: value["targets"][0].update(target_relative=bad_relative),
    )

    with pytest.raises(deployment_journal.JournalError, match="相对路径|不一致"):
        DeploymentEngine(state).get_batch(plan.batch_id)


def test_tampered_owned_backup_path_outside_root_is_never_cleaned(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    canary = source.parent / "outside-owned"
    put(canary, "canary.txt", b"keep")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    overwrite_journal(
        state, plan.batch_id,
        lambda value: value["targets"][0].update(backup_path=str(canary)),
    )

    result = DeploymentEngine(state).finalize(plan.batch_id)

    assert result.status == "failed"
    assert (canary / "canary.txt").read_bytes() == b"keep"
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}


def test_preview_includes_directories_type_changes_and_missing_target_root(layout):
    source, vault_a, vault_b, state = layout
    put(source, "same.txt", b"same")
    (source / "same-dir").mkdir()
    (source / "empty-added").mkdir()
    put(source / "flip", "child.txt", b"child")
    target = vault_a / "Config"
    put(target, "same.txt", b"same")
    (target / "same-dir").mkdir(parents=True)
    (target / "empty-removed").mkdir()
    put(target, "flip", b"was a file")
    engine = DeploymentEngine(state)

    existing = engine.analyze(request_for(source, [("a", vault_a)])).targets[0]
    changes = {item.relative_path: item for item in existing.changes}

    assert (changes["empty-added"].action, changes["empty-added"].kind) == ("add", "dir")
    assert (changes["empty-removed"].action, changes["empty-removed"].kind) == ("delete", "dir")
    assert (changes["flip"].action, changes["flip"].kind) == ("update", "dir")
    assert "file 变为 dir" in changes["flip"].reason
    assert changes["same-dir"].action == "skip"
    assert changes["same-dir"].will_replace_entity
    assert changes["same.txt"].will_replace_entity
    assert existing.replaces_tree

    missing = engine.analyze(request_for(source, [("b", vault_b)])).targets[0]
    root_change = next(item for item in missing.changes if item.relative_path == "")
    assert (root_change.action, root_change.kind) == ("add", "dir")
    assert missing.directory_counts["add"] >= 1


@pytest.mark.parametrize("operation", ["rollback", "finalize"])
def test_authenticated_grant_rejects_consistent_journal_forgery_before_source_read(
        layout, monkeypatch, operation):
    source, vault_a, _, state = layout
    put(source, "source-secret.txt", b"source must remain unread")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    source_manifest, source_identity = deployment._manifest_tree(str(source))
    source_volume = deployment.volume_identity(str(source))
    parent_identity = deployment.identity(deployment.snapshot(str(source.parent)))

    def forge(value):
        target = value["targets"][0]
        target.update({
            "source_path": str(source),
            "source_volume": source_volume,
            "target_root": str(source.parent),
            "target_relative": source.name,
            "target_path": str(source),
            "target_volume": source_volume,
            "target_root_identity": list(parent_identity),
            "phase": "committed",
            "stage_path": None,
            "stage_identity": None,
            "backup_path": None,
            "backup_identity": None,
            "rollback_path": None,
            "rollback_identity": None,
            "deployed_identity": list(source_identity),
            "original_identity": None,
            "source_manifest": source_manifest,
            "original_manifest": None,
            "deployed_manifest": source_manifest,
            "created_parents": [],
        })

    overwrite_journal(state, plan.batch_id, forge)
    source_key = os.path.normcase(str(source.resolve()))
    original_open = builtins.open
    original_scandir = os.scandir
    original_lstat = deployment_journal.snapshot

    def inside_source(path_value) -> bool:
        key = os.path.normcase(deployment.canonical(path_value))
        try:
            return os.path.normcase(os.path.commonpath((source_key, key))) == source_key
        except ValueError:
            return False

    def guarded_open(path_value, *args, **kwargs):
        assert not inside_source(path_value), "forged source was opened before grant rejection"
        return original_open(path_value, *args, **kwargs)

    def guarded_scandir(path_value):
        assert not inside_source(path_value), "forged source was scanned before grant rejection"
        return original_scandir(path_value)

    def guarded_snapshot(path_value):
        assert not inside_source(path_value), "forged source was stated before grant rejection"
        return original_lstat(path_value)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "scandir", guarded_scandir)
    monkeypatch.setattr(deployment_journal, "snapshot", guarded_snapshot)

    result = getattr(DeploymentEngine(state), operation)(plan.batch_id)

    assert result.status == "failed"
    assert any("认证" in message or "授权" in message for message in result.errors)
    assert original_open(source / "source-secret.txt", "rb").read() == b"source must remain unread"


@pytest.mark.parametrize("tamper", ["anchor", "key"])
def test_changed_grant_anchor_or_replaced_key_is_rejected(layout, tamper):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    if tamper == "anchor":
        anchor = state / "deployment-grants" / f"{plan.batch_id}.grant.json"
        value = json.loads(anchor.read_text(encoding="utf-8"))
        value["grant"]["label"] = "tampered"
        anchor.write_text(json.dumps(value), encoding="utf-8")
    else:
        (state / "deployment-journal.key").write_bytes(os.urandom(32))

    with pytest.raises(deployment_journal.JournalError, match="认证|授权"):
        DeploymentEngine(state).get_batch(plan.batch_id)


def test_state_directory_uses_resolved_existing_ancestor_for_overlap_check(layout, monkeypatch):
    source, vault_a, _, _ = layout
    put(source, "value.txt", b"value")
    alias_parent = source.parent / "state-alias-parent"
    alias_parent.mkdir()
    requested = alias_parent / "state"
    original_realpath = os.path.realpath
    alias_key = os.path.normcase(str(alias_parent.resolve()))
    resolved_source = str(source.resolve())

    def resolve_alias(path_value):
        canonical_path = deployment.canonical(path_value)
        if os.path.normcase(canonical_path) == alias_key:
            return resolved_source
        return original_realpath(path_value)

    monkeypatch.setattr(os.path, "realpath", resolve_alias)
    engine = DeploymentEngine(requested)

    assert os.path.normcase(engine.state_dir) == os.path.normcase(str(source / "state"))
    with pytest.raises(deployment.SyncError, match="状态目录|数据目录"):
        engine.analyze(request_for(source, [("a", vault_a)]))


def test_two_interprocess_lock_instances_cannot_overlap_and_release_cleanly(layout):
    _, vault_a, _, state = layout
    engine = DeploymentEngine(state)
    first = deployment._InterprocessLockSet(engine.state_dir, (str(vault_a),))
    second = deployment._InterprocessLockSet(engine.state_dir, (str(vault_a),))
    first.acquire()
    try:
        with pytest.raises(DeploymentError, match="另一个 ObManage 进程"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_windows_deployment_mutex_namespace_is_cross_session():
    name = deployment._deployment_mutex_name("state:C:/example")

    assert name.startswith("Global\\ObManage.Deployment.")
    assert not name.startswith("Local\\")


def test_finalize_retries_after_verified_backup_was_partly_cleaned(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old-a.txt", b"a")
    put(vault_a / "Config", "old-b.txt", b"b")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    original_unlink = deployment._unlink_owned_file
    calls = 0

    def fail_after_one(path_value, expected):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected cleanup failure")
        return original_unlink(path_value, expected)

    monkeypatch.setattr(deployment, "_unlink_owned_file", fail_after_one)
    first = engine.finalize(plan.batch_id)
    assert first.status == "failed"
    assert engine.get_batch(plan.batch_id).status == "finalize_required"

    monkeypatch.setattr(deployment, "_unlink_owned_file", original_unlink)
    second = DeploymentEngine(state).finalize(plan.batch_id)

    assert second.success
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}


def test_rollback_retries_after_verified_quarantine_was_partly_cleaned(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new-a.txt", b"a")
    put(source, "new-b.txt", b"b")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    original_unlink = deployment._unlink_owned_file
    calls = 0

    def fail_after_one(path_value, expected):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected rollback cleanup failure")
        return original_unlink(path_value, expected)

    monkeypatch.setattr(deployment, "_unlink_owned_file", fail_after_one)
    first = engine.rollback(plan.batch_id)
    assert first.status == "failed"
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"old"}

    monkeypatch.setattr(deployment, "_unlink_owned_file", original_unlink)
    second = DeploymentEngine(state).rollback(plan.batch_id)

    assert second.success
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"old"}


def test_identityless_owned_stage_in_corrupt_journal_never_authorizes_cleanup(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    target = engine.get_batch(plan.batch_id).targets[0]
    stage = Path(target.stage_path or "")
    put(stage, "new.txt", b"competitor")
    overwrite_journal(
        state, plan.batch_id,
        lambda value: value["targets"][0].update(stage_identity=None),
    )

    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.status == "failed"
    assert (stage / "new.txt").read_bytes() == b"competitor"


def test_identityless_created_parent_in_corrupt_journal_is_never_removed(tmp_path):
    template = tmp_path / "template"
    put(template / "File" / "Templater", "new.md", b"new")
    target = tmp_path / "target"
    target.mkdir()
    state = tmp_path / "state"
    request = DeploymentRequest((DeploymentSelection(
        DeploymentComponent.templater(template), DeploymentTarget("target", str(target))
    ),))
    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    assert record.created_parents

    def remove_identity(value):
        value["targets"][0]["created_parents"][0]["identity"] = None

    overwrite_journal(state, plan.batch_id, remove_identity)
    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.status == "failed"
    assert (target / "File" / "Templater" / "new.md").read_bytes() == b"new"
    assert (target / "File").is_dir()


@pytest.mark.parametrize("field,value", [
    ("status", "invented-status"),
    ("phase", "invented-phase"),
])
def test_corrupt_status_or_phase_makes_journal_listing_fail_closed(layout, field, value):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success

    def corrupt(document):
        if field == "status":
            document["status"] = value
        else:
            document["targets"][0]["phase"] = value

    overwrite_journal(state, plan.batch_id, corrupt)

    with pytest.raises(deployment_journal.JournalError, match="状态|阶段"):
        DeploymentEngine(state).list_batches()


@pytest.mark.parametrize("corruption,match", [
    ("empty", "目标列表"),
    ("duplicate-selection", "重复的目标编号"),
    ("overlapping-target", "相同或互相包含"),
])
def test_corrupt_target_set_makes_journal_listing_fail_closed(
        layout, corruption, match):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success

    def corrupt(document):
        if corruption == "empty":
            document["targets"] = []
            return
        clone = dict(document["targets"][0])
        if corruption == "overlapping-target":
            clone["selection_id"] = "00000000-0000-0000-0000-000000000001"
            clone["target_relative"] = "Config/Nested"
            clone["target_path"] = str(vault_a / "Config" / "Nested")
        document["targets"].append(clone)

    overwrite_journal(state, plan.batch_id, corrupt)

    with pytest.raises(deployment_journal.JournalError, match=match):
        DeploymentEngine(state).list_batches()


@pytest.mark.parametrize("field", ["status", "phase"])
def test_valid_but_inconsistent_terminal_state_tamper_cannot_skip_rollback(
        layout, field):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    backup = Path(engine.get_batch(plan.batch_id).targets[0].backup_path or "")

    def corrupt(document):
        if field == "status":
            document["status"] = "rolled_back"
        else:
            document["targets"][0]["phase"] = "rolled_back"

    overwrite_journal(state, plan.batch_id, corrupt)
    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.status == "failed"
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}
    assert backup.exists()


def test_single_dynamic_identity_tamper_fails_whole_record_authentication(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    backup = Path(engine.get_batch(plan.batch_id).targets[0].backup_path or "")

    def corrupt(document):
        identity_value = document["targets"][0]["backup_identity"]
        identity_value[2] += 1

    overwrite_journal(state, plan.batch_id, corrupt)
    result = DeploymentEngine(state).finalize(plan.batch_id)

    assert result.status == "failed"
    assert any("认证" in message for message in result.errors)
    assert backup.exists()


def test_created_parent_injection_cannot_delete_preexisting_empty_parent(tmp_path):
    template = tmp_path / "template"
    put(template / "File" / "Templater", "new.md", b"new")
    target = tmp_path / "target"
    existing_parent = target / "File"
    existing_parent.mkdir(parents=True)
    state = tmp_path / "state"
    request = DeploymentRequest((DeploymentSelection(
        DeploymentComponent.templater(template), DeploymentTarget("target", str(target))
    ),))
    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    assert engine.execute(plan).success
    assert engine.get_batch(plan.batch_id).targets[0].created_parents == ()
    parent_identity = deployment.identity(deployment.snapshot(str(existing_parent)))

    def inject(document):
        document["targets"][0]["created_parents"] = [{
            "path": str(existing_parent), "identity": list(parent_identity),
        }]

    overwrite_journal(state, plan.batch_id, inject)
    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.status == "failed"
    assert existing_parent.is_dir()
    assert (existing_parent / "Templater" / "new.md").read_bytes() == b"new"


def test_owned_path_must_equal_exact_deterministic_name(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    backup = Path(record.backup_path or "")
    decoy = backup.with_name(backup.name.removesuffix(".backup") + ".user-data.backup")
    shutil.copytree(backup, decoy)
    decoy_identity = deployment.identity(deployment.snapshot(str(decoy)))
    engine.journal.set_target(
        plan.batch_id, record.selection_id,
        backup_path=str(decoy), backup_identity=decoy_identity,
    )

    result = DeploymentEngine(state).finalize(plan.batch_id)

    assert result.status == "failed"
    assert any("名称无效" in message for message in result.errors)
    assert decoy.exists()
    assert backup.exists()
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}


def test_unknown_added_after_cleanup_manifest_check_is_never_deleted(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    backup = Path(engine.get_batch(plan.batch_id).targets[0].backup_path or "")
    original_scan = deployment._scan_tree
    backup_key = os.path.normcase(str(backup.resolve()))
    backup_scans = 0

    def inject_after_authorized_scan(root, *args, **kwargs):
        nonlocal backup_scans
        tree = original_scan(root, *args, **kwargs)
        if os.path.normcase(deployment.canonical(root)) == backup_key:
            backup_scans += 1
            if backup_scans == 2:
                put(backup, "unknown.txt", b"must survive")
        return tree

    monkeypatch.setattr(deployment, "_scan_tree", inject_after_authorized_scan)
    result = engine.finalize(plan.batch_id)

    assert result.status == "failed"
    assert (backup / "unknown.txt").read_bytes() == b"must survive"
    assert (backup / "old.txt").read_bytes() == b"old"


@pytest.mark.parametrize("component_kind", ["obsidian", "templater"])
def test_state_directory_inside_user_selected_source_root_is_rejected(
        tmp_path, component_kind):
    source_vault = tmp_path / f"source-{component_kind}"
    target = tmp_path / f"target-{component_kind}"
    target.mkdir()
    if component_kind == "obsidian":
        put(source_vault / ".obsidian", "config.json", b"value")
        component = DeploymentComponent.obsidian(source_vault)
    else:
        put(source_vault / "File" / "Templater", "note.md", b"value")
        component = DeploymentComponent.templater(source_vault)
    state = source_vault / "state-sibling"
    request = DeploymentRequest((DeploymentSelection(
        component, DeploymentTarget("target", str(target))
    ),))

    with pytest.raises(DeploymentError, match="状态目录|数据目录"):
        DeploymentEngine(state).analyze(request)

    assert not state.exists()


def test_analysis_rejects_entry_added_after_directory_enumeration(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "first.txt", b"first")
    original_hash = deployment._hash_file
    injected = False

    def add_late_entry(path, expected, cancel):
        nonlocal injected
        digest = original_hash(path, expected, cancel)
        if not injected and Path(path).parent == source:
            injected = True
            put(source, "late.txt", b"late")
        return digest

    monkeypatch.setattr(deployment, "_hash_file", add_late_entry)
    with pytest.raises(DeploymentError, match="枚举发生变化|目录发生变化"):
        DeploymentEngine(state).analyze(request_for(source, [("a", vault_a)]))

    assert not state.exists()


def test_execute_rejects_late_entry_during_final_source_scan(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "first.txt", b"first")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    original_hash = deployment._hash_file
    source_hashes = 0

    def add_on_commit_revalidation(path, expected, cancel):
        nonlocal source_hashes
        digest = original_hash(path, expected, cancel)
        if Path(path).parent == source:
            source_hashes += 1
            if source_hashes == 5:
                put(source, "late.txt", b"late")
        return digest

    monkeypatch.setattr(deployment, "_hash_file", add_on_commit_revalidation)
    result = engine.execute(plan)

    assert result.status == "failed"
    assert source_hashes >= 5
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"old"}
    assert (source / "late.txt").read_bytes() == b"late"
    assert engine.get_batch(plan.batch_id).status == "rolled_back"


def test_missing_key_is_not_recreated_when_existing_grants_need_recovery(layout):
    source, vault_a, vault_b, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    put(vault_b / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    first = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(first).success
    key_path = state / "deployment-journal.key"
    key_path.unlink()

    second_engine = DeploymentEngine(state)
    second = second_engine.analyze(request_for(source, [("b", vault_b)]))
    result = second_engine.execute(second)

    assert result.status == "failed"
    assert any("密钥缺失" in message for message in result.errors)
    assert not key_path.exists()
    assert tree_bytes(vault_b / "Config") == {"old.txt": b"old"}


def test_pending_batch_blocks_new_deployment_before_target_write(layout):
    source, vault_a, vault_b, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"a")
    put(vault_b / "Config", "old.txt", b"b")
    engine = DeploymentEngine(state)
    first = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(first).success
    journals = state / "deployment-journal"
    grants = state / "deployment-grants"
    before_journals = {path.name for path in journals.iterdir()}
    before_grants = {path.name for path in grants.iterdir()}
    second = engine.analyze(request_for(source, [("b", vault_b)]))

    result = engine.execute(second)

    assert result.status == "failed"
    assert any("待恢复部署批次" in message for message in result.errors)
    assert tree_bytes(vault_b / "Config") == {"old.txt": b"b"}
    assert {path.name for path in journals.iterdir()} == before_journals
    assert {path.name for path in grants.iterdir()} == before_grants


def test_replaced_full_length_key_blocks_all_new_batches(layout):
    source, vault_a, vault_b, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"a")
    put(vault_b / "Config", "old.txt", b"b")
    first_engine = DeploymentEngine(state)
    first = first_engine.analyze(request_for(source, [("a", vault_a)]))
    assert first_engine.execute(first).success
    journals = state / "deployment-journal"
    grants = state / "deployment-grants"
    before_journals = {path.name for path in journals.iterdir()}
    before_grants = {path.name for path in grants.iterdir()}
    key_path = state / "deployment-journal.key"
    replacement = os.urandom(deployment_journal.KEY_SIZE)
    assert replacement != key_path.read_bytes()
    key_path.write_bytes(replacement)

    second_engine = DeploymentEngine(state)
    second = second_engine.analyze(request_for(source, [("b", vault_b)]))
    result = second_engine.execute(second)

    assert result.status == "failed"
    assert any("认证失败" in message or "完整性" in message for message in result.errors)
    assert tree_bytes(vault_b / "Config") == {"old.txt": b"b"}
    assert {path.name for path in journals.iterdir()} == before_journals
    assert {path.name for path in grants.iterdir()} == before_grants
    assert not (journals / f"{second.batch_id}.json").exists()
    assert not (grants / f"{second.batch_id}.grant.json").exists()


@pytest.mark.parametrize("orphan_kind", ["journal", "grant"])
def test_orphan_journal_or_grant_makes_listing_fail_closed(tmp_path, orphan_kind):
    state = tmp_path / "state"
    batch_id = "00000000-0000-0000-0000-000000000001"
    if orphan_kind == "journal":
        directory = state / "deployment-journal"
        name = f"{batch_id}.json"
    else:
        directory = state / "deployment-grants"
        name = f"{batch_id}.grant.json"
    directory.mkdir(parents=True)
    (directory / name).write_text("{}", encoding="utf-8")

    with pytest.raises(deployment_journal.JournalError, match="不完整"):
        DeploymentEngine(state).list_batches()


def test_damaged_key_is_visible_to_empty_history_listing(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "deployment-journal.key").write_bytes(b"partial")

    with pytest.raises(deployment_journal.JournalError, match="密钥无效"):
        DeploymentEngine(state).list_batches()


def test_damaged_key_can_be_repaired_only_when_no_authority_exists(layout):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    state.mkdir()
    key_path = state / "deployment-journal.key"
    key_path.write_bytes(b"partial")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))

    result = engine.execute(plan)

    assert result.success
    assert len(key_path.read_bytes()) == deployment_journal.KEY_SIZE
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}


def test_key_publication_race_never_overwrites_and_validates_the_winner(
        layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    key_path = state / "deployment-journal.key"

    def publish_invalid_competitor(_source, target):
        Path(target).write_bytes(b"competing-invalid-key")
        raise FileExistsError("simulated competing publisher")

    publication_name = "rename" if os.name == "nt" else "link"
    monkeypatch.setattr(deployment_journal.os, publication_name,
                        publish_invalid_competitor)

    result = engine.execute(plan)

    assert result.status == "failed"
    assert key_path.read_bytes() == b"competing-invalid-key"
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"old"}
    assert not tuple((state / "deployment-journal").iterdir())
    assert not tuple((state / "deployment-grants").iterdir())


def test_interrupted_first_key_write_never_publishes_short_key_or_authority(
        layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    original_write = deployment_journal.os.write
    interrupted = False

    def fail_during_key_write(descriptor, value):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            original_write(descriptor, value[:1])
            raise OSError("simulated interrupted key write")
        return original_write(descriptor, value)

    monkeypatch.setattr(deployment_journal.os, "write", fail_during_key_write)
    result = engine.execute(plan)

    assert result.status == "failed"
    assert interrupted
    assert tree_bytes(vault_a / "Config") == {"old.txt": b"old"}
    assert not (state / "deployment-journal.key").exists()
    assert not tuple((state / "deployment-journal").iterdir())
    assert not tuple((state / "deployment-grants").iterdir())
    assert not tuple(state.glob("deployment-journal.key.tmp-*"))


def test_commit_recovery_marks_pending_before_automatic_rollback(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success

    def crash_before_rollback(_batch_id, *, automatic=False):
        raise SystemExit("simulated crash")

    monkeypatch.setattr(engine, "_rollback_locked", crash_before_rollback)
    with pytest.raises(SystemExit, match="simulated crash"):
        engine._recover_failed_execution(plan.batch_id, "injected failure", cancelled=False)

    assert engine.get_batch(plan.batch_id).status == "rollback_required"


def test_unregistered_crash_parent_is_retained_without_blocking_rollback(tmp_path):
    source = tmp_path / "source"
    put(source, "new.txt", b"new")
    target = tmp_path / "target"
    target.mkdir()
    state = tmp_path / "state"
    request = DeploymentRequest((DeploymentSelection(
        DeploymentComponent.direct("nested", source, "A/B/Config"),
        DeploymentTarget("target", str(target)),
    ),))
    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id).targets[0]
    assert [Path(item.path).name for item in record.created_parents] == ["A", "B"]
    # Model a hard crash after B was created but before its identity was
    # appended: A is authorized, while B must be treated as unknown.
    engine.journal.set_target(
        plan.batch_id, record.selection_id,
        created_parents=(record.created_parents[0],),
    )

    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.success
    assert result.journal_status == "rolled_back_with_residuals"
    assert not (target / "A" / "B" / "Config").exists()
    assert (target / "A" / "B").is_dir()
    batch = engine.get_batch(plan.batch_id)
    assert batch.status == "rolled_back_with_residuals"
    assert any("已保留" in message for message in batch.errors)


def test_residual_warning_survives_a_later_target_rollback_failure(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    put(source, "new.txt", b"new")
    failing_root = tmp_path / "failing-target"
    residual_root = tmp_path / "residual-target"
    failing_root.mkdir()
    residual_root.mkdir()
    state = tmp_path / "state"
    component = DeploymentComponent.direct("nested", source, "A/B/Config")
    request = DeploymentRequest((
        # Rollback runs in reverse order, so the residual target completes
        # before the failing target exercises durable warning retention.
        DeploymentSelection(component, DeploymentTarget("failing", str(failing_root))),
        DeploymentSelection(component, DeploymentTarget("residual", str(residual_root))),
    ))
    engine = DeploymentEngine(state)
    plan = engine.analyze(request)
    assert engine.execute(plan).success
    record = engine.get_batch(plan.batch_id)
    failing = next(item for item in record.targets if item.target_root == str(failing_root))
    residual = next(item for item in record.targets if item.target_root == str(residual_root))
    engine.journal.set_target(
        plan.batch_id, residual.selection_id,
        created_parents=(residual.created_parents[0],),
    )
    original_remove = deployment._remove_owned_tree
    injected = False

    def fail_second_core_cleanup(path, *args, **kwargs):
        nonlocal injected
        if failing.selection_id.replace("-", "") in Path(path).name and str(path).endswith(".rollback"):
            injected = True
            raise DeploymentError("simulated later rollback failure")
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(deployment, "_remove_owned_tree", fail_second_core_cleanup)
    first_attempt = DeploymentEngine(state).rollback(plan.batch_id)

    assert first_attempt.status == "failed"
    assert injected
    interrupted = engine.get_batch(plan.batch_id)
    persisted_residual = next(
        item for item in interrupted.targets if item.selection_id == residual.selection_id
    )
    assert persisted_residual.phase == "rolled_back"
    assert any("已保留" in warning for warning in persisted_residual.residuals)

    monkeypatch.setattr(deployment, "_remove_owned_tree", original_remove)
    retried = DeploymentEngine(state).rollback(plan.batch_id)

    assert retried.success
    assert retried.journal_status == "rolled_back_with_residuals"
    completed = engine.get_batch(plan.batch_id)
    assert any("已保留" in warning for warning in completed.errors)


def test_install_rename_identity_replacement_is_never_adopted(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    original_rename = deployment._rename_directory
    displaced = source.parent / "displaced-installed-tree"

    def replace_after_install(source_path, target_path):
        original_rename(source_path, target_path)
        if str(source_path).endswith(".stage"):
            original_rename(target_path, str(displaced))
            shutil.copytree(displaced, target_path)

    monkeypatch.setattr(deployment, "_rename_directory", replace_after_install)
    result = engine.execute(plan)

    assert result.status == "failed"
    assert tree_bytes(vault_a / "Config") == {"new.txt": b"new"}
    assert displaced.exists()
    assert engine.get_batch(plan.batch_id).status == "rollback_blocked"


def test_rollback_rename_identity_replacement_is_never_adopted(layout, monkeypatch):
    source, vault_a, _, state = layout
    put(source, "new.txt", b"new")
    put(vault_a / "Config", "old.txt", b"old")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request_for(source, [("a", vault_a)]))
    assert engine.execute(plan).success
    original_rename = deployment._rename_directory
    displaced = source.parent / "displaced-rollback-tree"

    def replace_after_quarantine(source_path, target_path):
        original_rename(source_path, target_path)
        if str(target_path).endswith(".rollback"):
            original_rename(target_path, str(displaced))
            shutil.copytree(displaced, target_path)

    monkeypatch.setattr(deployment, "_rename_directory", replace_after_quarantine)
    result = DeploymentEngine(state).rollback(plan.batch_id)

    assert result.status == "failed"
    assert displaced.exists()
    rollback_path = Path(engine.get_batch(plan.batch_id).targets[0].rollback_path or "")
    assert rollback_path.exists()
    assert tree_bytes(rollback_path) == {"new.txt": b"new"}
