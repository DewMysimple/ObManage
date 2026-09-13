from __future__ import annotations

import hashlib
import json
import os
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
    assert not state_dir.exists(), "Analysis must not create application state"
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
    assert len(preview.tree_sha256) == 64


def test_only_explicit_real_vault_root_trash_is_considered(tmp_path):
    vault = make_vault(tmp_path, "vault")
    root_trash = put(vault / ".trash", "remove.md", b"remove")
    business_trash = put(vault, "projects/.trash/keep.md", b"keep")
    non_vault = tmp_path / "ordinary"
    put(non_vault, ".trash/also-keep.md", b"keep")

    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault, non_vault])
    result = engine.execute(plan, [vault])

    assert [entry.relative_path for entry in plan.vaults[0].entries] == ["remove.md"]
    assert len(plan.issues) == 1
    assert result.status == "success"
    assert result.operation_id is None
    assert not root_trash.exists()
    assert business_trash.read_bytes() == b"keep"
    assert (non_vault / ".trash/also-keep.md").read_bytes() == b"keep"
    assert (vault / ".trash").is_dir()


def test_invalid_or_missing_vault_markers_are_reported_without_writes(tmp_path):
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / ".obsidian").write_text("not a directory", encoding="utf-8")
    (fake / ".trash").mkdir()
    missing_trash = tmp_path / "missing"
    (missing_trash / ".obsidian").mkdir(parents=True)

    plan = TrashCleanupEngine(tmp_path / "state").analyze([fake, missing_trash])

    assert not plan.vaults
    assert {issue.code for issue in plan.issues} == {"unsafe", "missing_trash"}
    assert not (missing_trash / ".trash").exists()
    assert not (tmp_path / "state").exists()


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
    assert result.bytes_freed == 0
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


def test_only_selected_vault_is_directly_cleaned_without_quarantine_state(tmp_path):
    first = make_vault(tmp_path, "first")
    second = make_vault(tmp_path, "second")
    first_file = put(first / ".trash", "first.md", b"first")
    second_file = put(second / ".trash", "second.md", b"second")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([first, second])

    result = engine.execute(plan, [first])

    assert result.status == "success"
    assert result.operation_id is None
    assert result.bytes_freed == len(b"first")
    assert not first_file.exists()
    assert list((first / ".trash").iterdir()) == []
    assert second_file.read_bytes() == b"second"
    assert not (state_dir / "trash_backups").exists()
    assert not (state_dir / "trash_journal").exists()
    assert not (state_dir / "trash-auth.key").exists()
    assert (state_dir / "trash-operation.lock").is_file()


def test_readonly_regular_file_is_safely_cleaned(tmp_path):
    vault = make_vault(tmp_path, "vault")
    readonly = put(vault / ".trash", "readonly.md", b"readonly")
    readonly.chmod(stat.S_IREAD)
    engine = TrashCleanupEngine(tmp_path / "state")

    result = engine.execute(engine.analyze([vault]), [vault])

    assert result.status == "success"
    assert result.bytes_freed == len(b"readonly")
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
    assert result.bytes_freed == 0
    assert locked.read_bytes() == b"locked"
    assert chmod_targets == []


def test_partial_direct_delete_reports_only_items_actually_removed(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    good = put(vault / ".trash", "good.md", b"good")
    locked = put(vault / ".trash", "locked.md", b"locked")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    real_unlink = trash_module._unlink_file

    def fail_one(path: str, expected):
        if Path(path).name == "locked.md":
            raise PermissionError("locked for test")
        return real_unlink(path, expected)

    monkeypatch.setattr(trash_module, "_unlink_file", fail_one)
    result = engine.execute(plan, [vault])

    assert result.status == "partial"
    assert result.operation_id is None
    assert result.bytes_freed == 4
    assert result.vaults[0].removed_files == 1
    assert result.vaults[0].removed_bytes == 4
    assert not good.exists()
    assert locked.read_bytes() == b"locked"
    assert any(Path(failure.path).name == "locked.md" for failure in result.failures)
    assert not (tmp_path / "state/trash_backups").exists()


def test_structural_failure_mid_clear_preserves_irreversible_progress(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    first = put(vault / ".trash", "a.md", b"a")
    second = put(vault / ".trash", "b.md", b"bb")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    real_read_snapshot = trash_module._read_snapshot

    def fail_after_first_delete(path: str, kind: str | None = None):
        if canonical(path) == canonical(vault / ".trash") and not first.exists():
            raise TrashSafetyError("simulated trash identity failure")
        return real_read_snapshot(path, kind)

    monkeypatch.setattr(trash_module, "_read_snapshot", fail_after_first_delete)
    result = engine.execute(plan, [vault])

    assert result.status == "partial"
    assert result.bytes_freed == 1
    assert result.vaults[0].removed_files == 1
    assert result.vaults[0].removed_bytes == 1
    assert not first.exists()
    assert second.read_bytes() == b"bb"
    assert any("identity failure" in failure.message for failure in result.failures)


def test_cancellation_before_revalidation_removes_nothing(tmp_path):
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    cancelled = Event()
    cancelled.set()

    result = engine.execute(plan, [vault], cancel=cancelled)

    assert result.status == "cancelled"
    assert result.bytes_freed == 0
    assert item.read_bytes() == b"keep"


def test_cancellation_during_direct_clear_reports_irreversible_progress(tmp_path):
    vault = make_vault(tmp_path, "vault")
    first = put(vault / ".trash", "a.md", b"aaaa")
    second = put(vault / ".trash", "b.md", b"bbbbbb")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    cancelled = Event()

    def stop_after_first(event):
        if event.phase == "clear" and event.completed_bytes:
            cancelled.set()

    result = engine.execute(plan, [vault], cancel=cancelled, progress=stop_after_first)

    assert result.status == "cancelled"
    assert result.bytes_freed == 4
    assert not first.exists()
    assert second.read_bytes() == b"bbbbbb"
    assert result.vaults[0].removed_files == 1


def test_new_item_during_clear_is_never_adopted_as_deletion_authority(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "a.md", b"a")
    put(vault / ".trash", "b.md", b"bb")
    engine = TrashCleanupEngine(tmp_path / "state")
    plan = engine.analyze([vault])
    real_unlink = trash_module._unlink_file
    injected = False

    def unlink_then_inject(path: str, expected):
        nonlocal injected
        real_unlink(path, expected)
        if not injected:
            injected = True
            put(vault / ".trash", "new.md", b"new")

    monkeypatch.setattr(trash_module, "_unlink_file", unlink_then_inject)
    result = engine.execute(plan, [vault])

    assert result.status == "partial"
    assert result.bytes_freed == 3
    assert (vault / ".trash/new.md").read_bytes() == b"new"
    assert any("未预览" in failure.message for failure in result.failures)


def test_state_directory_may_not_be_inside_source_tree(tmp_path):
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(vault / ".trash" / "state")
    plan = engine.analyze([vault])

    result = engine.execute(plan, [vault])

    assert result.status == "rejected"
    assert item.read_bytes() == b"keep"
    assert not (vault / ".trash/state").exists()


def test_process_wide_lock_rejects_overlap_and_is_released(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])
    entered = Event()
    release = Event()
    original = TrashCleanupEngine._revalidate_all

    def hold(previews, cancel, progress):
        entered.set()
        assert release.wait(5)
        return original(previews, cancel, progress)

    monkeypatch.setattr(TrashCleanupEngine, "_revalidate_all", staticmethod(hold))
    completed = []
    thread = Thread(target=lambda: completed.append(engine.execute(plan, [vault])))
    thread.start()
    assert entered.wait(5)

    overlapping = TrashCleanupEngine(state_dir).execute(plan, [vault])
    release.set()
    thread.join(5)

    assert overlapping.status == "rejected"
    assert "另一个仓库管理任务" in overlapping.failures[0].message
    assert completed[0].status == "success"


def test_cross_process_legacy_lock_rejects_overlap_and_crash_releases_it(
    tmp_path, seed_legacy_quarantine
):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, vault)
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
        cwd=Path(__file__).parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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

    resumed = TrashCleanupEngine(state_dir).finalize(operation_id)
    assert resumed.status == "success"
    assert not backup.exists()


def test_pending_legacy_quarantine_blocks_direct_cleanup_until_finalized(
    tmp_path, seed_legacy_quarantine
):
    legacy_vault = make_vault(tmp_path, "legacy")
    put(legacy_vault / ".trash", "old.md", b"old")
    direct_vault = make_vault(tmp_path, "direct")
    direct_item = put(direct_vault / ".trash", "new.md", b"new")
    state_dir = tmp_path / "state"
    operation_id, _ = seed_legacy_quarantine(state_dir, legacy_vault)
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([direct_vault])

    blocked = engine.execute(plan, [direct_vault])

    assert blocked.status == "rejected"
    assert "旧版回收站隔离批次" in blocked.failures[0].message
    assert direct_item.read_bytes() == b"new"

    finalized = engine.finalize(operation_id)
    cleaned = engine.execute(plan, [direct_vault])
    assert finalized.status == "success"
    assert cleaned.status == "success"
    assert not direct_item.exists()


def test_legacy_quarantine_can_still_restore_and_finalize_after_upgrade(
    tmp_path, seed_legacy_quarantine
):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "note.md", b"note")
    put(vault / ".trash", "folder/data.bin", b"data")
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, vault)

    operations = TrashCleanupEngine(state_dir).list_operations()
    restored = TrashCleanupEngine(state_dir).restore(operation_id)
    finalized = TrashCleanupEngine(state_dir).finalize(operation_id)

    assert [item.operation_id for item in operations] == [operation_id]
    assert restored.status == "success"
    assert (vault / ".trash/note.md").read_bytes() == b"note"
    assert (vault / ".trash/folder/data.bin").read_bytes() == b"data"
    assert finalized.status == "success"
    assert finalized.bytes_freed == 8
    assert not backup.exists()
    assert TrashCleanupEngine(state_dir).get_operation(operation_id).records[0].status == "finalized"


def test_tampered_legacy_backup_blocks_restore_without_touching_target(
    tmp_path, seed_legacy_quarantine
):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "original.md", b"original")
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, vault)
    (backup / "original.md").write_bytes(b"tampered")

    restored = TrashCleanupEngine(state_dir).restore(operation_id)

    assert restored.status == "rejected"
    assert list((vault / ".trash").iterdir()) == []


def test_legacy_journal_authentication_prevents_restore_redirection(
    tmp_path, seed_legacy_quarantine
):
    vault = make_vault(tmp_path, "source")
    put(vault / ".trash", "original.md", b"original")
    redirected = make_vault(tmp_path, "redirected")
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, vault)
    journal_path = state_dir / "trash_journal" / f"{operation_id}.json"
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    data["vaults"][0]["preview"]["vault_root"] = str(redirected)
    data["vaults"][0]["preview"]["trash_path"] = str(redirected / ".trash")
    journal_path.write_text(json.dumps(data), encoding="utf-8")

    restored = TrashCleanupEngine(state_dir).restore(operation_id)

    assert restored.status == "rejected"
    assert list((redirected / ".trash").iterdir()) == []
    assert (backup / "original.md").read_bytes() == b"original"


@pytest.mark.parametrize("replacement", [None, b"partial-key"])
def test_missing_or_partial_legacy_key_blocks_listing_and_direct_cleanup(
    tmp_path, seed_legacy_quarantine, replacement
):
    legacy = make_vault(tmp_path, "legacy")
    put(legacy / ".trash", "old.md", b"old")
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, legacy)
    key_path = state_dir / "trash-auth.key"
    if replacement is None:
        key_path.unlink()
    else:
        key_path.write_bytes(replacement)
    direct = make_vault(tmp_path, "direct")
    direct_item = put(direct / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([direct])

    with pytest.raises(TrashSafetyError):
        engine.list_operations()
    result = engine.execute(plan, [direct])

    assert result.status == "rejected"
    assert direct_item.read_bytes() == b"keep"
    assert (backup / "old.md").read_bytes() == b"old"
    assert (state_dir / "trash_journal" / f"{operation_id}.json").is_file()
    assert (not key_path.exists()) if replacement is None else key_path.read_bytes() == replacement


@pytest.mark.parametrize("change", ["conflict", "trash_replaced", "marker_replaced"])
def test_legacy_restore_rejects_changed_target_identity_or_content(
    tmp_path, seed_legacy_quarantine, change
):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "old.md", b"old")
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, vault)
    if change == "conflict":
        put(vault / ".trash", "new.md", b"new")
    elif change == "trash_replaced":
        (vault / ".trash").rmdir()
        (vault / ".trash").mkdir()
    else:
        (vault / ".obsidian").rmdir()
        (vault / ".obsidian").mkdir()

    restored = TrashCleanupEngine(state_dir).restore(operation_id)

    assert restored.status == "rejected"
    assert (backup / "old.md").read_bytes() == b"old"
    assert not (vault / ".trash/old.md").exists()
    if change == "conflict":
        assert (vault / ".trash/new.md").read_bytes() == b"new"


def test_legacy_finalize_partial_retry_counts_each_byte_once(
    tmp_path, monkeypatch, seed_legacy_quarantine
):
    vault = make_vault(tmp_path, "vault")
    put(vault / ".trash", "a.md", b"aaaa")
    put(vault / ".trash", "b.md", b"bbbbbb")
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, vault)
    operation_root = backup.parent
    real_unlink = trash_module._unlink_file
    failed = False

    def fail_second(path: str, expected):
        nonlocal failed
        if canonical(path) == canonical(backup / "b.md") and not failed:
            failed = True
            raise PermissionError("second unlink failed for test")
        return real_unlink(path, expected)

    monkeypatch.setattr(trash_module, "_unlink_file", fail_second)
    first = TrashCleanupEngine(state_dir).finalize(operation_id)

    assert first.status == "partial"
    assert first.bytes_freed == 4
    first_record = TrashCleanupEngine(state_dir).get_operation(operation_id).records[0]
    assert first_record.status == "finalize_partial"
    assert first_record.freed_bytes == 4
    assert not (backup / "a.md").exists()
    assert (backup / "b.md").read_bytes() == b"bbbbbb"

    monkeypatch.setattr(trash_module, "_unlink_file", real_unlink)
    second = TrashCleanupEngine(state_dir).finalize(operation_id)
    final_record = TrashCleanupEngine(state_dir).get_operation(operation_id).records[0]
    assert second.status == "success"
    assert second.bytes_freed == 6
    assert final_record.status == "finalized"
    assert final_record.freed_bytes == 10
    assert first.bytes_freed + second.bytes_freed == 10
    assert not operation_root.exists()


def test_legacy_finalize_rejects_unknown_item_injected_after_preflight(
    tmp_path, monkeypatch, seed_legacy_quarantine
):
    vault = make_vault(tmp_path, "vault")
    payload = b"authenticated backup"
    put(vault / ".trash", "note.md", payload)
    state_dir = tmp_path / "state"
    operation_id, backup = seed_legacy_quarantine(state_dir, vault)
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
    assert TrashCleanupEngine(state_dir).get_operation(operation_id).records[0].status == "finalizing"

    unknown.unlink()
    monkeypatch.setattr(trash_module, "_remove_backup_tree", real_remove)
    retry = TrashCleanupEngine(state_dir).finalize(operation_id)
    assert retry.status == "success"
    assert retry.bytes_freed == len(payload)
    assert not backup.exists()


def test_corrupt_legacy_journal_fails_closed_for_listing_and_direct_cleanup(tmp_path):
    state_dir = tmp_path / "state"
    journal_dir = state_dir / "trash_journal"
    journal_dir.mkdir(parents=True)
    operation_id = str(uuid.uuid4())
    (journal_dir / f"{operation_id}.json").write_text("{broken", encoding="utf-8")
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])

    with pytest.raises(TrashSafetyError):
        engine.list_operations()
    result = engine.execute(plan, [vault])

    assert result.status == "rejected"
    assert item.read_bytes() == b"keep"


def test_orphaned_legacy_backup_fails_closed_for_listing_and_direct_cleanup(tmp_path):
    state_dir = tmp_path / "state"
    orphan_id = str(uuid.uuid4())
    put(state_dir / "trash_backups" / orphan_id, "vault_0000/old.md", b"old")
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])

    with pytest.raises(TrashSafetyError, match="缺少认证记录"):
        engine.list_operations()
    result = engine.execute(plan, [vault])

    assert result.status == "rejected"
    assert result.bytes_freed == 0
    assert item.read_bytes() == b"keep"
    assert (state_dir / "trash_backups" / orphan_id / "vault_0000/old.md").read_bytes() == b"old"


def test_unknown_legacy_journal_temp_fails_closed_before_direct_cleanup(tmp_path):
    state_dir = tmp_path / "state"
    journal_dir = state_dir / "trash_journal"
    journal_dir.mkdir(parents=True)
    temp_name = f"{uuid.uuid4()}.json.tmp-{uuid.uuid4()}"
    (journal_dir / temp_name).write_text("partial", encoding="utf-8")
    vault = make_vault(tmp_path, "vault")
    item = put(vault / ".trash", "keep.md", b"keep")
    engine = TrashCleanupEngine(state_dir)
    plan = engine.analyze([vault])

    with pytest.raises(TrashSafetyError, match="未知项目"):
        engine.list_operations()
    result = engine.execute(plan, [vault])

    assert result.status == "rejected"
    assert result.bytes_freed == 0
    assert item.read_bytes() == b"keep"
