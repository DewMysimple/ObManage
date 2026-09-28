from dataclasses import replace
from pathlib import Path
import os
import threading

import pytest

from obmanage.management import deployment
from obmanage.management.comsync import components, DatedVault, newest_vaults, with_creation_times
from obmanage.management.deployment import DeploymentEngine, DeploymentRequest, DeploymentSelection, DeploymentTarget
from obmanage.management.models import VaultCatalogResult, VaultInfo


def put(root, name, value="new"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def contents(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
            for p in root.rglob("*") if p.is_file()}


def request(source, targets, chosen=("claude", "claudian", "obsidian", "templater", "file")):
    return DeploymentRequest(tuple(DeploymentSelection(component, DeploymentTarget(str(i), str(target)))
        for i, target in enumerate(targets) for component in components(str(source), chosen)),
        label="obmanage-ui:comsync")


@pytest.fixture
def scope(tmp_path):
    source, first, second = (tmp_path / name for name in ("source", "first", "second"))
    for name in (".claude/rules.md", ".claudian/settings.json", ".obsidian/app.json", "File/Templater/template.md"):
        put(source, name)
        put(first, name, "old")
    put(source, "File/Note/private.md", "never copy")
    put(source, "File/Attachment/private.txt", "never copy")
    put(source, "File/Other/private.txt", "never copy")
    put(first, "File/Note/keep.md", "keep note")
    put(first, "File/Attachment/keep.txt", "keep attachment")
    put(first, "File/Other/keep.txt", "keep other")
    put(first, "File/Templater/obsolete.md", "remove")
    (second / ".obsidian").mkdir(parents=True)
    return source, first, second, tmp_path / "state"


@pytest.mark.parametrize("completion", ["rollback", "finalize"])
def test_all_components_new_and_existing_vaults_full_transaction(scope, completion, monkeypatch):
    source, first, second, state = scope
    source_before, target_before = contents(source), contents(first)
    engine = DeploymentEngine(state)
    original_scan = deployment._scan_tree
    def scan(root, *args, **kwargs):
        if Path(root).is_relative_to(source) or Path(root).is_relative_to(first):
            assert Path(root).name not in {"Note", "Attachment", "Other"}
        return original_scan(root, *args, **kwargs)
    monkeypatch.setattr(deployment, "_scan_tree", scan)
    plan = engine.analyze(request(source, (first, second)))
    assert not state.exists()
    assert contents(source) == source_before and contents(first) == target_before
    assert len(plan.targets) == 12  # Templater only once per target, two directory intents.
    dirs = [item for item in plan.targets if item.ensure_directory]
    assert all(len(item.changes) == 1 and item.changes[0].action in {"add", "skip"} for item in dirs)
    result = engine.execute(plan)
    assert result.success, result.errors
    assert contents(source) == source_before
    for path in ("File/Note/keep.md", "File/Attachment/keep.txt", "File/Other/keep.txt"):
        assert contents(first)[str(Path(path))] == target_before[str(Path(path))]
    for target in (first, second):
        for name in (".claude/rules.md", ".claudian/settings.json", ".obsidian/app.json", "File/Templater/template.md"):
            assert (target / name).read_text() == "new"
    assert not (first / "File/Templater/obsolete.md").exists()
    for name in ("Note", "Attachment"):
        assert list((second / "File" / name).iterdir()) == []
    assert not (second / "File/Other").exists()
    result = getattr(DeploymentEngine(state), completion)(plan.batch_id)
    assert result.success, result.errors
    if completion == "rollback":
        assert contents(first) == target_before
        assert not (second / "File").exists()
        assert (second / ".obsidian").is_dir()


def test_directory_creation_does_not_require_source_note_or_attachment(scope):
    source, first, second, state = scope
    # Use a different source that has only templates.
    source = source.parent / "templates-only"
    put(source, "File/Templater/hello.md")
    engine = DeploymentEngine(state)
    plan = engine.analyze(request(source, (second,), ("file",)))
    assert engine.execute(plan).success
    assert (second / "File/Note").is_dir() and (second / "File/Attachment").is_dir()
    assert engine.finalize(plan.batch_id).success


@pytest.mark.parametrize("chosen,destinations", [
    (("claude",), {".claude"}), (("claudian",), {".claudian"}),
    (("obsidian",), {".obsidian"}), (("templater",), {"File/Templater"}),
    (("file",), {"File/Templater", "File/Note", "File/Attachment"}),
    (("file", "templater"), {"File/Templater", "File/Note", "File/Attachment"}),
])
def test_independent_component_selection(scope, chosen, destinations):
    source, first, second, state = scope
    engine = DeploymentEngine(state)
    plan = engine.analyze(request(source, (second,), chosen))
    assert {Path(item.target_path).relative_to(second).as_posix() for item in plan.targets} == destinations
    assert engine.execute(plan).success
    assert engine.rollback(plan.batch_id).success


@pytest.mark.parametrize("phase", ["prepare", "commit", "cancel"])
def test_all_target_faults_rollback_directory_intents(scope, monkeypatch, phase):
    source, first, second, state = scope
    before = contents(first)
    engine = DeploymentEngine(state)
    plan = engine.analyze(request(source, (second, first)))
    cancel = threading.Event()
    if phase == "prepare":
        original = engine._copy_to_stage
        def fail(plan, item, *args):
            if item.target_root == str(first):
                raise OSError("injected stage failure")
            return original(plan, item, *args)
        monkeypatch.setattr(engine, "_copy_to_stage", fail)
    def progress(event):
        if event.phase == "committed" and event.component_id == "file_note":
            if phase == "commit":
                raise OSError("injected commit failure")
            if phase == "cancel":
                cancel.set()
    result = engine.execute(plan, cancel=cancel, progress=progress)
    assert not result.success
    assert contents(first) == before
    assert not (second / "File").exists()


@pytest.mark.parametrize("change", ["late_directory", "file_collision", "source_change", "volume_change"])
def test_stale_preview_refuses_before_writing(scope, monkeypatch, change):
    source, first, second, state = scope
    engine = DeploymentEngine(state)
    plan = engine.analyze(request(source, (second,), ("file",)))
    if change == "late_directory":
        put(second, "File/Note/new.md", "keep")
    elif change == "file_collision":
        put(second, "File/Note", "keep")
    elif change == "source_change":
        put(source, "File/Templater/template.md", "changed")
    else:
        monkeypatch.setattr(deployment, "volume_identity", lambda path: "different-volume")
    before = contents(second)
    result = engine.execute(plan)
    assert not result.success
    assert contents(second) == before
    assert not state.exists()


def test_existing_directory_contents_can_change_without_entering_sync_scope(scope):
    source, first, second, state = scope
    engine = DeploymentEngine(state)
    plan = engine.analyze(request(source, (first,), ("file",)))
    put(first, "File/Note/added-after-preview.md", "keep")
    assert engine.execute(plan).success
    assert engine.finalize(plan.batch_id).success
    assert (first / "File/Note/added-after-preview.md").read_text() == "keep"


def test_new_directory_receiving_user_content_blocks_rollback_without_deleting_it(scope):
    source, first, second, state = scope
    engine = DeploymentEngine(state)
    plan = engine.analyze(request(source, (second,), ("file",)))
    assert engine.execute(plan).success
    put(second, "File/Note/user.md", "keep")
    result = DeploymentEngine(state).rollback(plan.batch_id)
    assert not result.success
    assert (second / "File/Note/user.md").read_text() == "keep"
    preview = engine.inspect_recovery(plan.batch_id)
    assert preview.can_rollback
    recovered = engine.rollback(plan.batch_id, preview=preview)
    assert recovered.success, recovered.errors
    note = next(item for item in engine.get_batch(plan.batch_id).targets if item.component_id == "file_note")
    assert (Path(note.rollback_path) / "user.md").read_text() == "keep"
    assert not (second / "File/Note").exists()


def test_creation_time_ties_and_unknown_are_explicit(tmp_path):
    older = DatedVault("a", "a", created_ns=10)
    newer = DatedVault("b", "b", created_ns=20)
    tie = DatedVault("c", "c", created_ns=20)
    assert newest_vaults((older, newer, tie)) == (newer, tie)
    assert newest_vaults((older, VaultInfo("unknown", "unknown"))) == ()
    vault = tmp_path / "vault"
    vault.mkdir()
    result = with_creation_times(VaultCatalogResult((VaultInfo(str(vault), "vault"),)))
    if os.name == "nt":
        assert result.vaults[0].created_ns == vault.stat().st_birthtime_ns
        assert not result.issues
    missing = with_creation_times(VaultCatalogResult((VaultInfo(str(vault / "missing"), "missing"),)))
    assert missing.issues and missing.vaults[0].created_ns is None



@pytest.mark.parametrize("kind", ["missing-template", "file-instead-of-directory", "target-link"])
def test_component_boundaries_fail_closed(scope, kind):
    source, first, second, state = scope
    if kind == "missing-template":
        source = source.parent / "empty-source"
        source.mkdir()
    elif kind == "file-instead-of-directory":
        put(second, "File/Note", "keep")
    else:
        (second / "File").mkdir()
        try:
            (second / "File/Note").symlink_to(source / "File/Note", target_is_directory=True)
        except OSError:
            pytest.skip("symlink privileges unavailable")
    with pytest.raises(Exception, match="目录|链接|重解析"):
        DeploymentEngine(state).analyze(request(source, (second,), ("file",)))
    assert not state.exists()
