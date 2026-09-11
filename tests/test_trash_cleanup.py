from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from threading import Event, Thread

import pytest

import obmanage.management.trash as trash_module
from obmanage.management.trash import TrashCleanupEngine, TrashSafetyError
from obmanage.models import SyncCancelled
from obmanage.paths import canonical


def make_vault(base: Path, name: str) -> Path:
    root = base / name
    (root / ".obsidian").mkdir(parents=True)
    (root / ".trash").mkdir()
    return root


def put(root: Path, relative: str, content: bytes) -> Path:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def visible_tree(root: Path) -> dict[str, tuple[str, bytes | None]]:
    if not root.exists():
        return {}
    result: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("link", None)
        elif path.is_dir():
            result[relative] = ("dir", None)
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
    return result


def test_analysis_lists_every_item_with_full_hash_and_writes_nothing(tmp_path):
    vault = make_vault(tmp_path, "vault")
    note = put(vault / ".trash", "note.md", b"note")
    image = put(vault / ".trash", "folder/image.bin", b"\x00\x01\x02")
    state_dir = tmp_path / "state"
    before = visible_tree(vault)

    plan = TrashCleanupEngine(state_dir).analyze([vault])

    assert not plan.issues
    assert not state_dir.exists(), "Analysis must not even create the application state directory"
    assert visible_tree(vault) == before
    preview = plan.vaults[0]
    assert preview.file_count == 2
    assert preview.dir_count == 1
    assert preview.total_bytes == 7
    entries = {entry.relative_path: entry for entry in preview.entries}
    assert entries["note.md"].sha256 == hashlib.sha256(note.read_bytes()).hexdigest()
    assert entries["folder/image.bin"].sha256 == hashlib.sha256(image.read_bytes()).hexdigest()
    assert entries["folder"].kind == "dir"
    assert entries["folder"].size == 3
    assert len(entries["folder"].sha256) == 64
    assert entries["note.md"].snapshot.inode
    assert len(preview.tree_sha256) == 64


def test_only_explicit_real_vault_root_trash_is_considered(tmp_path):
    vault = make_vault(tmp_path, "vault")
    root_trash = put(vault / ".trash", "remove.md", b"remove")
    business_trash = put(vault, "projects/.trash/keep.md", b"keep")
    non_vault = tmp_path / "ordinary"
    put(non_vault, ".trash/also-keep.md", b"keep")

    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault, non_vault])

    assert [entry.relative_path for entry in plan.vaults[0].entries] == ["remove.md"]
    assert len(plan.issues) == 1
    assert "ordinary" in plan.issues[0].vault_root
    result = engine.execute(plan, [vault])
    assert result.status == "success"
    assert not root_trash.exists()
    assert business_trash.read_bytes() == b"keep"
    assert (non_vault / ".trash/also-keep.md").read_bytes() == b"keep"
    assert (vault / ".trash").is_dir()


def test_obsidian_marker_must_be_a_real_directory(tmp_path):
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / ".obsidian").write_text("not a directory", encoding="utf-8")
    (fake / ".trash").mkdir()

    plan = TrashCleanupEngine(tmp_path / "state").analyze([fake])

    assert not plan.vaults
    assert len(plan.issues) == 1
    assert plan.issues[0].code == "unsafe"


def test_missing_root_trash_is_reported_without_creating_it(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)

    plan = TrashCleanupEngine(tmp_path / "state").analyze([vault])

    assert not plan.vaults
    assert plan.issues[0].code == "missing_trash"
    assert not (vault / ".trash").exists()


def test_link_in_trash_is_rejected_and_external_target_is_untouched(tmp_path):
    vault = make_vault(tmp_path, "vault")
    external = put(tmp_path, "external/secret.md", b"secret")
    link = vault / ".trash" / "outside.md"
    try:
        os.symlink(external, link)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    plan = TrashCleanupEngine(tmp_path / "state").analyze([vault])

    assert not plan.vaults
    assert len(plan.issues) == 1
    assert plan.issues[0].code == "unsafe"
    assert external.read_bytes() == b"secret"


def test_any_stale_selected_vault_rejects_entire_operation_before_deletion(tmp_path):
    first = make_vault(tmp_path, "first")
    second = make_vault(tmp_path, "second")
    first_file = put(first / ".trash", "first.md", b"first")
    second_file = put(second / ".trash", "second.md", b"second")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([first, second])
    added = put(second / ".trash", "new-after-preview.md", b"new")

    result = engine.execute(plan, [first, second])

    assert result.status == "rejected"
    assert first_file.read_bytes() == b"first"
    assert second_file.read_bytes() == b"second"
    assert added.read_bytes() == b"new"
    assert not (tmp_path / "state/trash_backups").exists()
    assert not (tmp_path / "state/trash_journal").exists()


def test_execute_requires_an_explicit_previewed_selection(tmp_path):
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])

    empty = engine.execute(plan, [])
    unknown = engine.execute(plan, [tmp_path / "other"])

    assert empty.status == "rejected"
    assert unknown.status == "rejected"
    assert item.read_bytes() == b"keep"
    assert not (tmp_path / "state").exists()


def test_only_selected_vault_is_cleaned_and_backup_is_verified(tmp_path):
    first = make_vault(tmp_path, "first")
    second = make_vault(tmp_path, "second")
    first_file = put(first / ".trash", "first.md", b"first")
    second_file = put(second / ".trash", "second.md", b"second")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([first, second])

    result = engine.execute(plan, [first])

    assert result.status == "success"
    assert result.bytes_freed == 0
    assert not first_file.exists()
    assert second_file.read_bytes() == b"second"
    operation = TrashCleanupEngine(state_dir).get_operation(result.operation_id or "")
    assert len(operation.records) == 1
    record = operation.records[0]
    assert record.status == "success"
    assert (Path(record.backup_path) / "first.md").read_bytes() == b"first"


def test_readonly_regular_file_is_safely_cleaned(tmp_path):
    vault = make_vault(tmp_path, "vault")
    readonly = put(vault / ".trash", "readonly.md", b"readonly")
    readonly.chmod(stat.S_IREAD)
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])

    result = engine.execute(plan, [vault])

    assert result.status == "success"
    assert not readonly.exists()
    assert (vault / ".trash").is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows access handling is platform-specific")
def test_non_readonly_access_denied_never_changes_target_attributes(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    locked = put(vault / ".trash", "locked.md", b"locked")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    real_unlink = trash_module.os.unlink
    real_chmod = trash_module.os.chmod
    chmod_targets: list[str] = []

    def deny_locked(path):
        if canonical(path) == canonical(locked):
            raise PermissionError(13, "access denied", str(locked), 5)
        return real_unlink(path)

    def record_chmod(path, mode, *args, **kwargs):
        if canonical(path) == canonical(locked):
            chmod_targets.append(str(path))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(trash_module.os, "unlink", deny_locked)
    monkeypatch.setattr(trash_module.os, "chmod", record_chmod)

    result = engine.execute(plan, [vault])

    assert result.status == "partial"
    assert locked.read_bytes() == b"locked"
    assert chmod_targets == []


def test_backup_survives_process_boundary_restore_and_finalize(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    put(vault / ".trash", "folder/data.bin", b"data")
    state_dir = tmp_path / "state"
    plan = TrashCleanupEngine(state_dir).analyze([vault])

    cleaned = TrashCleanupEngine(state_dir).execute(plan, [vault])
    operation_id = cleaned.operation_id
    assert cleaned.status == "success"
    assert operation_id
    operation_root = Path(
        TrashCleanupEngine(state_dir).get_operation(operation_id).records[0].backup_path
    ).parent
    assert list((vault / ".trash").iterdir()) == []

    restored = TrashCleanupEngine(state_dir).restore(operation_id)
    assert restored.status == "success"
    assert (vault / ".trash/note.md").read_bytes() == b"note"
    assert (vault / ".trash/folder/data.bin").read_bytes() == b"data"

    finalized = TrashCleanupEngine(state_dir).finalize(operation_id)
    assert finalized.status == "success"
    assert finalized.bytes_freed == 8
    operation = TrashCleanupEngine(state_dir).get_operation(operation_id)
    assert operation.records[0].status == "finalized"
    assert not Path(operation.records[0].backup_path).exists()
    assert not operation_root.exists()
    refused = TrashCleanupEngine(state_dir).restore(operation_id)
    assert refused.status == "rejected"


def test_restore_refuses_target_conflicts_and_keeps_backup(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "old.md", b"old")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([vault]), [vault])
    new_item = put(vault / ".trash", "new.md", b"new")
    record = engine.get_operation(result.operation_id or "").records[0]

    restored = TrashCleanupEngine(state_dir).restore(result.operation_id or "")

    assert restored.status == "rejected"
    assert new_item.read_bytes() == b"new"
    assert (Path(record.backup_path) / "old.md").read_bytes() == b"old"
    assert not (vault / ".trash/old.md").exists()


def test_backup_tampering_blocks_restore_without_touching_target(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "original.md", b"original")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([vault]), [vault])
    record = engine.get_operation(result.operation_id or "").records[0]
    (Path(record.backup_path) / "original.md").write_bytes(b"tampered")

    restored = TrashCleanupEngine(state_dir).restore(result.operation_id or "")

    assert restored.status == "rejected"
    assert list((vault / ".trash").iterdir()) == []


def test_partial_delete_reports_only_items_actually_removed(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    good = put(vault / ".trash", "good.md", b"good")
    locked = put(vault / ".trash", "locked.md", b"locked")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    real_unlink = trash_module._unlink_file

    def fail_one(path: str, expected):
        if Path(path).name == "locked.md" and str(vault / ".trash") in path:
            raise PermissionError("locked for test")
        return real_unlink(path, expected)

    monkeypatch.setattr(trash_module, "_unlink_file", fail_one)
    result = engine.execute(plan, [vault])

    assert result.status == "partial"
    vault_result = result.vaults[0]
    assert vault_result.removed_files == 1
    assert vault_result.removed_bytes == 4
    assert not good.exists()
    assert locked.read_bytes() == b"locked"
    assert any(Path(failure.path).name == "locked.md" for failure in result.failures)
    record = engine.get_operation(result.operation_id or "").records[0]
    assert Path(record.backup_path, "good.md").read_bytes() == b"good"
    assert Path(record.backup_path, "locked.md").read_bytes() == b"locked"


def test_cancellation_during_backup_never_removes_repository_content(tmp_path):
    vault = make_vault(tmp_path, "vault")
    content = b"x" * (trash_module.HASH_CHUNK_SIZE * 2)
    item = put(vault / ".trash", "large.bin", content)
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    cancelled = Event()

    def stop(event):
        if event.phase == "backup" and event.completed_bytes:
            cancelled.set()

    result = engine.execute(plan, [vault], cancel=cancelled, progress=stop)

    assert result.status == "cancelled"
    assert item.read_bytes() == content
    assert list((vault / ".trash").iterdir()) == [item]


def test_state_directory_may_not_be_inside_source_tree(tmp_path):
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(vault / ".trash" / "state")
    plan = engine.analyze([vault])

    result = engine.execute(plan, [vault])

    assert result.status == "rejected"
    assert item.read_bytes() == b"keep"
    assert not (vault / ".trash/state").exists()


def test_moved_state_tree_inside_vault_is_rejected_before_recovery_lock_write(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "keep.md", b"keep")
    outside_state = tmp_path / "state"
    engine = TrashCleanupEngine(outside_state)
    cleaned = engine.execute(engine.analyze([vault]), [vault])
    assert cleaned.status == "success"
    operation_id = cleaned.operation_id or ""

    moved_state = vault / "moved-state"
    shutil.move(outside_state, moved_state)
    lock_path = moved_state / "trash-operation.lock"
    lock_before = lock_path.stat()
    moved_engine = TrashCleanupEngine(moved_state)
    backup = Path(moved_engine.get_operation(operation_id).records[0].backup_path)

    finalized = moved_engine.finalize(operation_id)

    assert finalized.status == "rejected"
    assert backup.is_dir()
    assert (backup / "keep.md").read_bytes() == b"keep"
    assert lock_path.stat().st_mtime_ns == lock_before.st_mtime_ns


def test_change_during_backup_is_detected_before_clear(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "note.md", b"before")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    real_copy = trash_module._copy_entries

    def copy_then_change(*args, **kwargs):
        result = real_copy(*args, **kwargs)
        if args[6] == "backup":
            item.write_bytes(b"after")
        return result

    monkeypatch.setattr(trash_module, "_copy_entries", copy_then_change)
    result = engine.execute(plan, [vault])

    assert result.status == "failed"
    assert item.read_bytes() == b"after"
    assert (vault / ".trash").is_dir()


def test_tampered_journal_cannot_redirect_restore(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([vault]), [vault])
    operation_id = result.operation_id or ""
    journal_path = state_dir / "trash_journal" / f"{operation_id}.json"
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    data["vaults"][0]["preview"]["trash_path"] = str(redirected)
    journal_path.write_text(json.dumps(data), encoding="utf-8")

    restored = TrashCleanupEngine(state_dir).restore(operation_id)

    assert restored.status == "rejected"
    assert list(redirected.iterdir()) == []
    assert list((vault / ".trash").iterdir()) == []


@pytest.mark.parametrize("field", ["count", "digest", "relative", "backup"])
def test_structurally_tampered_journal_is_rejected(tmp_path, field):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([vault]), [vault])
    operation_id = result.operation_id or ""
    journal_path = state_dir / "trash_journal" / f"{operation_id}.json"
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    if field == "count":
        data["vaults"][0]["preview"]["file_count"] += 1
    elif field == "digest":
        data["vaults"][0]["preview"]["tree_sha256"] = "0" * 64
    elif field == "relative":
        data["vaults"][0]["preview"]["entries"][0]["relative_path"] = "../escape.md"
    else:
        data["vaults"][0]["backup_rel"] = "../outside"
    journal_path.write_text(json.dumps(data), encoding="utf-8")

    restored = TrashCleanupEngine(state_dir).restore(operation_id)

    assert restored.status == "rejected"
    assert list((vault / ".trash").iterdir()) == []


def test_consistent_journal_redirection_without_hmac_key_is_rejected(tmp_path):
    source = make_vault(tmp_path, "source")
    redirected = make_vault(tmp_path, "redirected")
    put(source / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([source]), [source])
    operation_id = result.operation_id or ""
    redirected_preview = engine.analyze([redirected]).vaults[0]
    journal_path = state_dir / "trash_journal" / f"{operation_id}.json"
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    # Replace every mutually-consistent path/identity/manifest claim while
    # retaining the signature produced for the original operation.
    data["vaults"][0]["preview"] = trash_module._preview_to_json(redirected_preview)
    journal_path.write_text(json.dumps(data), encoding="utf-8")

    restored = TrashCleanupEngine(state_dir).restore(operation_id)

    assert restored.status == "rejected"
    assert list((redirected / ".trash").iterdir()) == []


@pytest.mark.parametrize("replacement", [None, b"partial-key"])
def test_missing_or_partial_key_with_existing_operation_blocks_new_execute(
    tmp_path, replacement
):
    first = make_vault(tmp_path, "first")
    put(first / ".trash", "first.md", b"first")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    old_result = engine.execute(engine.analyze([first]), [first])
    old_operation_id = old_result.operation_id or ""
    old_record = engine.get_operation(old_operation_id).records[0]
    old_backup = Path(old_record.backup_path) / "first.md"
    old_journal = state_dir / "trash_journal" / f"{old_operation_id}.json"
    old_journal_bytes = old_journal.read_bytes()
    key_path = state_dir / "trash-auth.key"
    key_path.unlink()
    if replacement is not None:
        key_path.write_bytes(replacement)

    second = make_vault(tmp_path, "second")
    second_item = put(second / ".trash", "second.md", b"second")
    second_engine = TrashCleanupEngine(state_dir)
    rejected = second_engine.execute(second_engine.analyze([second]), [second])

    assert rejected.status == "rejected"
    assert rejected.failures[0].phase == "preflight"
    assert second_item.read_bytes() == b"second"
    assert old_backup.read_bytes() == b"first"
    assert old_journal.read_bytes() == old_journal_bytes
    assert tuple((state_dir / "trash_journal").glob("*.json")) == (old_journal,)
    if replacement is None:
        assert not key_path.exists()
    else:
        assert key_path.read_bytes() == replacement


def test_interrupted_first_key_write_never_publishes_partial_key_or_deletes_source(
    tmp_path, monkeypatch
):
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "note.md", b"source remains")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])
    real_open = trash_module.os.open
    real_write = trash_module.os.write
    key_descriptor = None
    interrupted = False

    def track_key_descriptor(path, *args, **kwargs):
        nonlocal key_descriptor
        descriptor = real_open(path, *args, **kwargs)
        if "trash-auth.key.tmp-" in os.fspath(path):
            key_descriptor = descriptor
        return descriptor

    def interrupt_key_write(descriptor, data):
        nonlocal interrupted
        if descriptor == key_descriptor and not interrupted:
            interrupted = True
            real_write(descriptor, data[:7])
            raise OSError("simulated process interruption during key write")
        return real_write(descriptor, data)

    monkeypatch.setattr(trash_module.os, "open", track_key_descriptor)
    monkeypatch.setattr(trash_module.os, "write", interrupt_key_write)
    rejected = engine.execute(plan, [vault])

    assert rejected.status == "rejected"
    assert item.read_bytes() == b"source remains"
    assert not (state_dir / "trash-auth.key").exists()
    assert not tuple(state_dir.glob("trash-auth.key.tmp-*"))
    assert not tuple((state_dir / "trash_backups").iterdir())
    assert not tuple((state_dir / "trash_journal").iterdir())


def test_partial_legacy_key_is_repaired_only_when_no_operation_state_exists(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    key_path = state_dir / "trash-auth.key"
    key_path.write_bytes(b"partial-key")
    engine = TrashCleanupEngine(state_dir)

    result = engine.execute(engine.analyze([vault]), [vault])

    assert result.status == "success"
    assert len(key_path.read_bytes()) == trash_module.AUTH_KEY_SIZE
    assert key_path.read_bytes() != b"partial-key"


def test_replaced_empty_trash_directory_blocks_restore(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([vault]), [vault])
    old_trash = vault / ".trash"
    old_trash.rmdir()
    old_trash.mkdir()

    restored = TrashCleanupEngine(state_dir).restore(result.operation_id or "")

    assert restored.status == "rejected"
    assert list(old_trash.iterdir()) == []


def test_replaced_vault_root_blocks_restore(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([vault]), [vault])
    moved = tmp_path / "old-vault"
    vault.rename(moved)
    replacement = make_vault(tmp_path, "vault")

    restored = TrashCleanupEngine(state_dir).restore(result.operation_id or "")

    assert restored.status == "rejected"
    assert list((replacement / ".trash").iterdir()) == []


def test_replaced_obsidian_marker_blocks_restore(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    result = engine.execute(engine.analyze([vault]), [vault])
    marker = vault / ".obsidian"
    marker.rmdir()
    marker.mkdir()

    restored = TrashCleanupEngine(state_dir).restore(result.operation_id or "")

    assert restored.status == "rejected"
    assert list((vault / ".trash").iterdir()) == []


def test_process_wide_lock_rejects_overlap_and_is_released(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])
    entered = Event()
    release = Event()
    real_revalidate = TrashCleanupEngine._revalidate_all

    def hold_lock(previews, cancel, progress):
        entered.set()
        assert release.wait(5)
        return real_revalidate(previews, cancel, progress)

    monkeypatch.setattr(TrashCleanupEngine, "_revalidate_all", staticmethod(hold_lock))
    completed = []
    worker = Thread(target=lambda: completed.append(engine.execute(plan, [vault])))
    worker.start()
    assert entered.wait(5)

    overlapping = TrashCleanupEngine(state_dir).execute(plan, [vault])
    assert overlapping.status == "rejected"
    assert overlapping.failures[0].phase == "lock"
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert completed[0].status == "success"

    # The worker's normal completion must not strand the process-wide lock.
    refreshed = TrashCleanupEngine(state_dir).analyze([vault])
    assert len(refreshed.vaults) == 1


def test_process_wide_lock_is_released_after_cancellation(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    engine = TrashCleanupEngine(tmp_path / "state")
    cancelled = Event()
    cancelled.set()

    with pytest.raises(SyncCancelled):
        engine.analyze([vault], cancel=cancelled)

    assert len(engine.analyze([vault]).vaults) == 1


def test_cross_process_lock_rejects_overlap_and_crash_releases_it(tmp_path):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    cleaned = engine.execute(engine.analyze([vault]), [vault])
    operation_id = cleaned.operation_id or ""
    ready = tmp_path / "child-ready"
    child_code = """
import sys
from pathlib import Path
import obmanage.management.trash as module

real_remove = module._remove_backup_tree
def hold_lock(*args, **kwargs):
    Path(sys.argv[3]).write_text('ready', encoding='utf-8')
    sys.stdin.readline()
    return real_remove(*args, **kwargs)
module._remove_backup_tree = hold_lock
module.TrashCleanupEngine(sys.argv[1]).finalize(sys.argv[2])
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(state_dir), operation_id, str(ready)],
        cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        _stdout, stderr = process.communicate(timeout=5)
        pytest.fail(f"child did not acquire the operation lock: {stderr}")
    try:
        overlapping = TrashCleanupEngine(state_dir).restore(operation_id)
        assert overlapping.status == "rejected"
        assert overlapping.failures[0].phase == "lock"
    finally:
        process.kill()
        process.communicate(timeout=10)

    # The kernel releases the byte-range lock when the child crashes. The
    # authenticated `finalizing` record can then be safely resumed.
    resumed = TrashCleanupEngine(state_dir).finalize(operation_id)
    assert resumed.status == "success"
    assert not Path(engine.get_operation(operation_id).records[0].backup_path).exists()


def test_finalize_partial_manifest_retries_and_counts_each_freed_byte_once(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "a.md", b"aaaa")
    put(vault / ".trash", "b.md", b"bbbbbb")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    cleaned = engine.execute(engine.analyze([vault]), [vault])
    operation_id = cleaned.operation_id or ""
    record = engine.get_operation(operation_id).records[0]
    operation_root = Path(record.backup_path).parent
    real_unlink = trash_module._unlink_file
    failed = False

    def fail_second(path: str, expected):
        nonlocal failed
        if Path(path).name == "b.md" and not failed:
            failed = True
            raise PermissionError("second unlink failed for test")
        return real_unlink(path, expected)

    monkeypatch.setattr(trash_module, "_unlink_file", fail_second)
    first = TrashCleanupEngine(state_dir).finalize(operation_id)
    assert first.status == "partial"
    assert first.bytes_freed == 4
    first_record = engine.get_operation(operation_id).records[0]
    assert first_record.status == "finalize_partial"
    assert first_record.freed_bytes == 4
    assert not (Path(record.backup_path) / "a.md").exists()
    assert (Path(record.backup_path) / "b.md").read_bytes() == b"bbbbbb"

    monkeypatch.setattr(trash_module, "_unlink_file", real_unlink)
    second = TrashCleanupEngine(state_dir).finalize(operation_id)
    assert second.status == "success"
    assert second.bytes_freed == 6
    final_record = engine.get_operation(operation_id).records[0]
    assert final_record.status == "finalized"
    assert final_record.freed_bytes == 10
    assert first.bytes_freed + second.bytes_freed == 10
    assert not operation_root.exists()


def test_finalize_partial_rejects_unknown_backup_item(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "a.md", b"a")
    put(vault / ".trash", "b.md", b"b")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    cleaned = engine.execute(engine.analyze([vault]), [vault])
    operation_id = cleaned.operation_id or ""
    backup = Path(engine.get_operation(operation_id).records[0].backup_path)
    real_unlink = trash_module._unlink_file

    def fail_b(path: str, expected):
        if Path(path).name == "b.md":
            raise PermissionError("locked")
        return real_unlink(path, expected)

    monkeypatch.setattr(trash_module, "_unlink_file", fail_b)
    assert TrashCleanupEngine(state_dir).finalize(operation_id).status == "partial"
    put(backup, "unknown.md", b"unknown")
    monkeypatch.setattr(trash_module, "_unlink_file", real_unlink)

    retry = TrashCleanupEngine(state_dir).finalize(operation_id)

    assert retry.status == "rejected"
    assert (backup / "unknown.md").read_bytes() == b"unknown"


def test_finalize_rejects_unknown_injected_after_preflight_before_any_delete(
    tmp_path, monkeypatch
):
    vault = make_vault(tmp_path, "vault")
    payload = b"authenticated backup"
    put(vault / ".trash", "note.md", payload)
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    cleaned = engine.execute(engine.analyze([vault]), [vault])
    operation_id = cleaned.operation_id or ""
    backup = Path(engine.get_operation(operation_id).records[0].backup_path)
    real_remove = trash_module._remove_backup_tree
    entered_delete = Event()
    allow_delete = Event()
    completed = []

    def pause_after_preflight(*args, **kwargs):
        entered_delete.set()
        assert allow_delete.wait(5)
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(trash_module, "_remove_backup_tree", pause_after_preflight)
    worker = Thread(
        target=lambda: completed.append(TrashCleanupEngine(state_dir).finalize(operation_id))
    )
    worker.start()
    assert entered_delete.wait(5)
    unknown = put(backup, "injected-after-preflight.bin", b"do not delete")
    allow_delete.set()
    worker.join(10)

    assert not worker.is_alive()
    assert completed[0].status == "partial"
    assert completed[0].bytes_freed == 0
    assert (backup / "note.md").read_bytes() == payload
    assert unknown.read_bytes() == b"do not delete"
    assert any("认证清单之外" in failure.message for failure in completed[0].failures)
    assert engine.get_operation(operation_id).records[0].status == "finalizing"

    unknown.unlink()
    monkeypatch.setattr(trash_module, "_remove_backup_tree", real_remove)
    retry = TrashCleanupEngine(state_dir).finalize(operation_id)
    assert retry.status == "success"
    assert retry.bytes_freed == len(payload)
    assert not backup.exists()


def test_uncommitted_cleanup_preserves_unknown_injected_into_owned_backup(
    tmp_path, monkeypatch
):
    vault = make_vault(tmp_path, "vault")
    original = put(vault / ".trash", "note.md", b"original")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])
    real_copy = trash_module._copy_entries

    def copy_then_inject_unknown(*args, **kwargs):
        result = real_copy(*args, **kwargs)
        if args[6] == "backup":
            put(Path(args[1]), "injected-during-cleanup.bin", b"competitor")
            raise TrashSafetyError("force uncommitted cleanup")
        return result

    monkeypatch.setattr(trash_module, "_copy_entries", copy_then_inject_unknown)
    result = engine.execute(plan, [vault])

    assert result.status == "failed"
    assert original.read_bytes() == b"original"
    operation_roots = tuple((state_dir / "trash_backups").iterdir())
    assert len(operation_roots) == 1
    backup = operation_roots[0] / "vault_0000"
    assert (backup / "note.md").read_bytes() == b"original"
    assert (backup / "injected-during-cleanup.bin").read_bytes() == b"competitor"
    assert TrashCleanupEngine(state_dir).list_operations() == ()


def test_uncommitted_backup_replacement_is_never_deleted_as_owned(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    original = put(vault / ".trash", "note.md", b"original")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])
    real_copy = trash_module._copy_entries
    competitor = None

    def replace_owned_path(*args, **kwargs):
        nonlocal competitor
        result = real_copy(*args, **kwargs)
        if args[6] == "backup":
            destination = Path(args[1])
            shutil.rmtree(destination)
            destination.mkdir()
            competitor = put(destination, "competitor.md", b"competitor")
        return result

    monkeypatch.setattr(trash_module, "_copy_entries", replace_owned_path)
    result = engine.execute(plan, [vault])

    assert result.status == "failed"
    assert original.read_bytes() == b"original"
    assert competitor is not None and competitor.read_bytes() == b"competitor"
    assert TrashCleanupEngine(state_dir).list_operations() == ()


def test_list_operations_cross_instance_includes_pending_and_finalized(tmp_path):
    first = make_vault(tmp_path, "first")
    second = make_vault(tmp_path, "second")
    put(first / ".trash", "first.md", b"first")
    put(second / ".trash", "second.md", b"second")
    state_dir = tmp_path / "state"
    first_engine = TrashCleanupEngine(state_dir)
    first_result = first_engine.execute(first_engine.analyze([first]), [first])
    time.sleep(0.002)
    second_engine = TrashCleanupEngine(state_dir)
    second_result = second_engine.execute(second_engine.analyze([second]), [second])
    finalized = TrashCleanupEngine(state_dir).finalize(first_result.operation_id or "")
    assert finalized.status == "success"

    operations = TrashCleanupEngine(state_dir).list_operations()

    assert [item.operation_id for item in operations] == [
        second_result.operation_id, first_result.operation_id,
    ]
    by_id = {item.operation_id: item for item in operations}
    assert by_id[first_result.operation_id or ""].records[0].status == "finalized"
    assert by_id[second_result.operation_id or ""].records[0].status == "success"


def test_list_operations_fails_closed_for_corrupt_journal(tmp_path):
    state_dir = tmp_path / "state"
    journal_dir = state_dir / "trash_journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "not-a-uuid.json").write_text("{}", encoding="utf-8")

    with pytest.raises(TrashSafetyError):
        TrashCleanupEngine(state_dir).list_operations()


def test_list_operations_fails_closed_for_invalid_json_record(tmp_path):
    state_dir = tmp_path / "state"
    journal_dir = state_dir / "trash_journal"
    journal_dir.mkdir(parents=True)
    operation_id = str(uuid.uuid4())
    (journal_dir / f"{operation_id}.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(TrashSafetyError):
        TrashCleanupEngine(state_dir).list_operations()
