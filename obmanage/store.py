"""Per-volume, per-pair content equivalence records (outside both mirrors)."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
from pathlib import Path


class BaselineStore:
    def __init__(self, state_dir: str | Path):
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(directory / "baselines.sqlite3", timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._paths_by_pair: dict[str, dict[str, set[str]]] = {}
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS baselines (
                pair_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                source_state TEXT NOT NULL, target_state TEXT NOT NULL,
                digest TEXT NOT NULL,
                PRIMARY KEY (pair_id, relative_path)
            );
            CREATE TABLE IF NOT EXISTS owned_temps (
                path TEXT PRIMARY KEY, pair_id TEXT NOT NULL, identity TEXT NOT NULL
            );
        """)
        self.connection.commit()
        self._data_version = self.connection.execute("PRAGMA data_version").fetchone()[0]

    @staticmethod
    def _relative_key(path: str) -> str:
        return os.path.normcase(path)

    def _refresh_external_changes(self) -> None:
        data_version = self.connection.execute("PRAGMA data_version").fetchone()[0]
        if data_version != self._data_version:
            self._paths_by_pair.clear()
            self._data_version = data_version

    @contextmanager
    def _write_transaction(self):
        # Acquire the writer lock before validating the cache version. Another
        # process cannot insert a case variant between refresh and commit.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            data_version = self.connection.execute("PRAGMA data_version").fetchone()[0]
            if data_version != self._data_version:
                self._paths_by_pair.clear()
                raise sqlite3.OperationalError("校验记录已被另一个任务修改，请重新分析差异。")
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _paths(self, pair_id: str) -> dict[str, set[str]]:
        self._refresh_external_changes()
        cached = self._paths_by_pair.get(pair_id)
        if cached is not None:
            return cached
        cached = {}
        for path, in self.connection.execute(
                "SELECT relative_path FROM baselines WHERE pair_id = ?", (pair_id,)):
            cached.setdefault(self._relative_key(path), set()).add(path)
        self._paths_by_pair[pair_id] = cached
        return cached

    def close(self) -> None:
        self.connection.close()

    def records(self, pair_id: str) -> dict[str, tuple[dict, dict, str]]:
        self._refresh_external_changes()
        rows = list(self.connection.execute(
            "SELECT relative_path, source_state, target_state, digest "
            "FROM baselines WHERE pair_id = ?", (pair_id,)
        ))
        paths: dict[str, set[str]] = {}
        for path, *_ in rows:
            paths.setdefault(self._relative_key(path), set()).add(path)
        self._paths_by_pair[pair_id] = paths
        return {path: (json.loads(src), json.loads(dst), digest)
                for path, src, dst, digest in rows}

    def save(self, pair_id: str, path: str, source: dict, target: dict, digest: str) -> None:
        relative_key = self._relative_key(path)
        with self._write_transaction():
            known_paths = self._paths(pair_id)
            variants = set(known_paths.get(relative_key, ()))
            # Delete+insert keeps a case-only rename and the new verified record
            # atomic under Windows path semantics without changing the on-disk
            # schema used by older ObManage releases.
            self.connection.executemany(
                "DELETE FROM baselines WHERE pair_id = ? AND relative_path = ?",
                ((pair_id, variant) for variant in variants),
            )
            self.connection.execute(
                "INSERT INTO baselines "
                "(pair_id, relative_path, source_state, target_state, digest) "
                "VALUES (?, ?, ?, ?, ?)",
                (pair_id, path, json.dumps(source), json.dumps(target), digest),
            )
        known_paths[relative_key] = {path}
        # Commit each verified file: cancellation or a crash must not discard
        # previously completed first-adoption hash work.

    def invalidate(self, records: list[tuple[str, str]]) -> None:
        """Atomically remove equivalence claims, including case-only aliases."""
        requested = {(pair_id, self._relative_key(path)) for pair_id, path in records}
        if not requested:
            return
        known_by_pair: dict[str, dict[str, set[str]]] = {}
        with self._write_transaction():
            exact_records = set()
            for pair_id, relative_key in requested:
                known_paths = known_by_pair.get(pair_id)
                if known_paths is None:
                    known_paths = self._paths(pair_id)
                    known_by_pair[pair_id] = known_paths
                variants = known_paths.get(relative_key)
                if variants:
                    exact_records.update((pair_id, variant) for variant in variants)
            self.connection.executemany(
                "DELETE FROM baselines WHERE pair_id = ? AND relative_path = ?",
                exact_records,
            )
        for pair_id, relative_key in requested:
            known_by_pair[pair_id].pop(relative_key, None)

    def remove(self, pair_id: str, path: str) -> None:
        self.invalidate([(pair_id, path)])

    def register_temp(self, pair_id: str, path: str, identity: tuple) -> None:
        self.connection.execute("INSERT INTO owned_temps VALUES (?, ?, ?)",
                                (path, pair_id, json.dumps(identity)))
        self.connection.commit()

    def unregister_temp(self, path: str) -> None:
        self.connection.execute("DELETE FROM owned_temps WHERE path = ?", (path,))
        self.connection.commit()

    def finalize_copy(self, pair_id: str, path: str, source: dict, target: dict,
                      digest: str, temp_path: str) -> None:
        """Publish one verified baseline and retire its temp ownership atomically."""
        relative_key = self._relative_key(path)
        with self._write_transaction():
            known_paths = self._paths(pair_id)
            variants = set(known_paths.get(relative_key, ()))
            self.connection.executemany(
                "DELETE FROM baselines WHERE pair_id = ? AND relative_path = ?",
                ((pair_id, variant) for variant in variants),
            )
            self.connection.execute(
                "INSERT INTO baselines "
                "(pair_id, relative_path, source_state, target_state, digest) "
                "VALUES (?, ?, ?, ?, ?)",
                (pair_id, path, json.dumps(source), json.dumps(target), digest),
            )
            self.connection.execute(
                "DELETE FROM owned_temps WHERE path = ?", (temp_path,)
            )
        known_paths[relative_key] = {path}

    def temps(self, pair_id: str) -> list[tuple[str, tuple]]:
        return [(path, tuple(json.loads(record))) for path, record in
                self.connection.execute("SELECT path, identity FROM owned_temps WHERE pair_id = ?",
                                        (pair_id,))]
