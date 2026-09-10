"""Per-volume, per-pair content equivalence records (outside both mirrors)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class BaselineStore:
    def __init__(self, state_dir: str | Path):
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(directory / "baselines.sqlite3", timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
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

    def close(self) -> None:
        self.connection.close()

    def records(self, pair_id: str) -> dict[str, tuple[dict, dict, str]]:
        return {path: (json.loads(src), json.loads(dst), digest) for path, src, dst, digest in
                self.connection.execute("SELECT relative_path, source_state, target_state, digest "
                                        "FROM baselines WHERE pair_id = ?", (pair_id,))}

    def save(self, pair_id: str, path: str, source: dict, target: dict, digest: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO baselines VALUES (?, ?, ?, ?, ?)",
                                (pair_id, path, json.dumps(source), json.dumps(target), digest))
        # Commit each verified file: cancellation or a crash must not discard
        # previously completed first-adoption hash work.
        self.connection.commit()

    def remove(self, pair_id: str, path: str) -> None:
        self.connection.execute("DELETE FROM baselines WHERE pair_id = ? AND relative_path = ?",
                                (pair_id, path))
        self.connection.commit()

    def register_temp(self, pair_id: str, path: str, identity: tuple) -> None:
        self.connection.execute("INSERT INTO owned_temps VALUES (?, ?, ?)",
                                (path, pair_id, json.dumps(identity)))
        self.connection.commit()

    def unregister_temp(self, path: str) -> None:
        self.connection.execute("DELETE FROM owned_temps WHERE path = ?", (path,))
        self.connection.commit()

    def temps(self, pair_id: str) -> list[tuple[str, tuple]]:
        return [(path, tuple(json.loads(record))) for path, record in
                self.connection.execute("SELECT path, identity FROM owned_temps WHERE pair_id = ?",
                                        (pair_id,))]
