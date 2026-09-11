from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3

import pytest

from obmanage.store import BaselineStore


def file_state(marker: int) -> dict:
    return {
        "kind": "file",
        "size": marker,
        "mtime_ns": marker,
        "ctime_ns": marker,
        "inode": marker,
        "device": marker,
    }


def test_existing_five_column_database_remains_compatible(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = sqlite3.connect(state_dir / "baselines.sqlite3")
    database.executescript("""
        CREATE TABLE baselines (
            pair_id TEXT NOT NULL, relative_path TEXT NOT NULL,
            source_state TEXT NOT NULL, target_state TEXT NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY (pair_id, relative_path)
        );
        CREATE TABLE owned_temps (
            path TEXT PRIMARY KEY, pair_id TEXT NOT NULL, identity TEXT NOT NULL
        );
    """)
    source, target = file_state(1), file_state(2)
    database.execute(
        "INSERT INTO baselines VALUES (?, ?, ?, ?, ?)",
        ("pair", "notes/item.md", json.dumps(source), json.dumps(target), "digest"),
    )
    database.commit()
    database.close()

    store = BaselineStore(state_dir)
    try:
        assert store.records("pair") == {
            "notes/item.md": (source, target, "digest")
        }
        columns = {row[1] for row in store.connection.execute("PRAGMA table_info(baselines)")}
        assert columns == {
            "pair_id", "relative_path", "source_state", "target_state", "digest"
        }
    finally:
        store.close()
    # A 1.2.2-style five-value insert still works after the new code opened it.
    database = sqlite3.connect(state_dir / "baselines.sqlite3")
    database.execute(
        "INSERT OR REPLACE INTO baselines VALUES (?, ?, ?, ?, ?)",
        ("old-client", "other.md", json.dumps(source), json.dumps(target), "digest"),
    )
    database.commit()
    database.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_saving_case_only_variant_atomically_replaces_old_record(tmp_path):
    store = BaselineStore(tmp_path / "state")
    try:
        store.save("pair", "Notes/Item.md", file_state(1), file_state(2), "old")
        store.save("pair", "notes/item.md", file_state(3), file_state(4), "new")

        assert store.records("pair") == {
            "notes/item.md": (file_state(3), file_state(4), "new")
        }
    finally:
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_legacy_case_variants_are_both_removed_by_one_save(tmp_path):
    store = BaselineStore(tmp_path / "state")
    try:
        # Model rows written by releases that treated SQLite keys as
        # case-sensitive even though the filesystem is not.
        store.connection.execute(
            "INSERT INTO baselines VALUES (?, ?, ?, ?, ?)",
            ("pair", "Note.md", json.dumps(file_state(1)),
             json.dumps(file_state(2)), "first"),
        )
        store.connection.execute(
            "INSERT INTO baselines VALUES (?, ?, ?, ?, ?)",
            ("pair", "note.md", json.dumps(file_state(3)),
             json.dumps(file_state(4)), "second"),
        )
        store.connection.commit()
        store.records("pair")

        store.save("pair", "NOTE.md", file_state(5), file_state(6), "verified")

        assert store.records("pair") == {
            "NOTE.md": (file_state(5), file_state(6), "verified")
        }
    finally:
        store.close()


def test_invalidation_of_both_directions_is_one_transactional_operation(tmp_path):
    store = BaselineStore(tmp_path / "state")
    try:
        store.save("forward", "note.md", file_state(1), file_state(2), "same")
        store.save("reverse", "note.md", file_state(2), file_state(1), "same")

        store.invalidate([
            ("forward", "note.md"),
            ("reverse", "note.md"),
        ])

        assert store.records("forward") == {}
        assert store.records("reverse") == {}
    finally:
        store.close()


def test_invalidation_failure_rolls_back_both_directions(tmp_path):
    store = BaselineStore(tmp_path / "state")
    try:
        store.save("forward", "note.md", file_state(1), file_state(2), "same")
        store.save("reverse", "note.md", file_state(2), file_state(1), "same")
        store.connection.execute("""
            CREATE TRIGGER reject_reverse_invalidation
            BEFORE DELETE ON baselines
            WHEN OLD.pair_id = 'reverse'
            BEGIN
                SELECT RAISE(ABORT, 'simulated invalidation failure');
            END;
        """)
        store.connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="simulated invalidation failure"):
            store.invalidate([
                ("forward", "note.md"),
                ("reverse", "note.md"),
            ])

        assert set(store.records("forward")) == {"note.md"}
        assert set(store.records("reverse")) == {"note.md"}
    finally:
        store.close()


def test_commit_failure_explicitly_rolls_back_transaction(tmp_path):
    store = BaselineStore(tmp_path / "state")
    try:
        store.connection.execute("PRAGMA foreign_keys = ON")
        store.connection.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (
                parent_id INTEGER,
                FOREIGN KEY (parent_id) REFERENCES parent(id)
                    DEFERRABLE INITIALLY DEFERRED
            );
        """)

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with store._write_transaction():
                store.connection.execute("INSERT INTO child VALUES (1)")

        assert not store.connection.in_transaction
        assert list(store.connection.execute("SELECT * FROM child")) == []
    finally:
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_two_connections_cannot_leave_case_variants_after_save(tmp_path):
    state_dir = tmp_path / "state"
    first = BaselineStore(state_dir)
    second = BaselineStore(state_dir)
    try:
        assert first.records("pair") == {}
        assert second.records("pair") == {}
        first.save("pair", "Note.md", file_state(1), file_state(2), "first")

        with pytest.raises(sqlite3.OperationalError, match="另一个任务"):
            second.save("pair", "note.md", file_state(3), file_state(4), "second")
        assert second.records("pair") == {
            "Note.md": (file_state(1), file_state(2), "first")
        }
        second.save("pair", "note.md", file_state(3), file_state(4), "second")

        assert first.records("pair") == {
            "note.md": (file_state(3), file_state(4), "second")
        }
    finally:
        second.close()
        first.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_stale_connection_must_refresh_before_invalidating_variant(tmp_path):
    state_dir = tmp_path / "state"
    first = BaselineStore(state_dir)
    second = BaselineStore(state_dir)
    try:
        assert second.records("pair") == {}
        first.save("pair", "Note.md", file_state(1), file_state(2), "first")

        with pytest.raises(sqlite3.OperationalError, match="另一个任务"):
            second.invalidate([("pair", "note.md")])
        assert set(second.records("pair")) == {"Note.md"}
        second.invalidate([("pair", "note.md")])

        assert first.records("pair") == {}
    finally:
        second.close()
        first.close()


def test_older_connection_cannot_restore_newer_invalidated_claim(tmp_path):
    state_dir = tmp_path / "state"
    seed = BaselineStore(state_dir)
    seed.save("pair", "note.md", file_state(1), file_state(2), "same")
    seed.close()
    older = BaselineStore(state_dir)
    newer = BaselineStore(state_dir)
    try:
        assert set(older.records("pair")) == {"note.md"}
        assert set(newer.records("pair")) == {"note.md"}
        newer.invalidate([("pair", "note.md")])

        with pytest.raises(sqlite3.OperationalError, match="另一个任务"):
            older.save("pair", "note.md", file_state(1), file_state(2), "older-late-save")

        assert older.records("pair") == {}
        assert newer.records("pair") == {}
    finally:
        newer.close()
        older.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_external_write_after_invalidation_commit_is_detected(tmp_path):
    state_dir = tmp_path / "state"
    victim = BaselineStore(state_dir)
    other = BaselineStore(state_dir)
    try:
        victim.save("pair", "Note.md", file_state(1), file_state(2), "first")
        assert set(other.records("pair")) == {"Note.md"}
        original_transaction = victim._write_transaction

        @contextmanager
        def interleave_after_commit():
            with original_transaction():
                yield
            assert other.records("pair") == {}
            other.save("pair", "NOTE.md", file_state(3), file_state(4), "newer")

        victim._write_transaction = interleave_after_commit
        victim.invalidate([("pair", "note.md")])

        with pytest.raises(sqlite3.OperationalError, match="另一个任务"):
            victim.save("pair", "note.md", file_state(5), file_state(6), "stale-save")
        assert victim.records("pair") == {
            "NOTE.md": (file_state(3), file_state(4), "newer")
        }
    finally:
        other.close()
        victim.close()
