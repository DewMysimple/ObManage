"""Last agreed copies and delayed removals, separate from disposable mirror hash caches."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .models import SyncError
from .paths import canonical, lexical_child_path, native


def path_key(name: str) -> str:
    return os.path.normcase(name).replace("\\", "/")


@dataclass(frozen=True)
class Agreed:
    source_name: str
    target_name: str
    source: dict
    target: dict
    digest: str


class RealtimeStore:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(directory) / "realtime.sqlite3")
        try:
            self.db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS bindings (
                    token TEXT PRIMARY KEY, mode TEXT NOT NULL, roots TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agreed (
                    token TEXT NOT NULL REFERENCES bindings ON DELETE CASCADE,
                    path TEXT NOT NULL, source_name TEXT NOT NULL, target_name TEXT NOT NULL,
                    source TEXT NOT NULL, target TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(token, path));
                CREATE TABLE IF NOT EXISTS removals (
                    token TEXT NOT NULL REFERENCES bindings ON DELETE CASCADE,
                    path TEXT NOT NULL, observed REAL NOT NULL, signature TEXT NOT NULL,
                    PRIMARY KEY(token, path));
                CREATE TABLE IF NOT EXISTS recent_deletes (
                    token TEXT NOT NULL REFERENCES bindings ON DELETE CASCADE, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS deleted_by_time ON recent_deletes(token, at);
                CREATE TABLE IF NOT EXISTS stages (
                    token TEXT NOT NULL REFERENCES bindings ON DELETE CASCADE,
                    path TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(token, path));
                CREATE TABLE IF NOT EXISTS held_removals (
                    token TEXT NOT NULL REFERENCES bindings ON DELETE CASCADE,
                    path TEXT NOT NULL, PRIMARY KEY(token, path));
            """)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @staticmethod
    def binding_locator(directory, token, mode):
        path = (Path(directory) / "realtime.sqlite3").resolve()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT roots FROM bindings WHERE token=? AND mode=?", (token, mode)).fetchone()
        if row is None:
            raise SyncError("实时绑定记录缺失，请暂停并重新绑定。")
        return json.loads(row[0]).get("portable_locator")

    def bind(self, mode, roots):
        token = uuid.uuid4().hex
        self.db.execute("INSERT INTO bindings VALUES (?,?,?)", (token, mode, json.dumps(roots)))
        return token

    def roots(self, token, mode):
        row = self.db.execute("SELECT roots FROM bindings WHERE token=? AND mode=?", (token, mode)).fetchone()
        if row is None:
            raise SyncError("找不到此实时同步的绑定记录，请暂停后重新核对位置。")
        return json.loads(row[0])

    def records(self, token, scopes):
        select = "SELECT path,source_name,target_name,source,target,digest FROM agreed WHERE token=?"
        result = {}
        filters = []
        if scopes is None:
            filters.append(("", (token,)))
        else:
            for scope in scopes:
                key = path_key(scope)
                filters.append((" AND path=?", (token, key)))
                filters.append((" AND path>=? AND path<?", (token, key + "/", key + "0")))
        for clause, arguments in filters:
            for key, src, dst, a, b, digest in self.db.execute(select + clause, arguments):
                result[key] = Agreed(src, dst, json.loads(a), json.loads(b), digest)
        return result

    def target_directory(self, token, name):
        row = self.db.execute("SELECT target FROM agreed WHERE token=? AND path=?",
                              (token, path_key(name))).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, token, agreed):
        key = path_key(agreed.source_name)
        self.db.execute("INSERT OR REPLACE INTO agreed VALUES (?,?,?,?,?,?,?)",
                        (token, key, agreed.source_name, agreed.target_name,
                         json.dumps(agreed.source), json.dumps(agreed.target), agreed.digest))
        self.db.execute("DELETE FROM removals WHERE token=? AND path=?", (token, key))

    def forget(self, token, key):
        self.db.execute("DELETE FROM agreed WHERE token=? AND path=?", (token, key))
        self.db.execute("DELETE FROM removals WHERE token=? AND path=?", (token, key))
        self.db.execute("DELETE FROM held_removals WHERE token=? AND path=?", (token, key))

    def held(self, token):
        return {row[0] for row in self.db.execute("SELECT path FROM held_removals WHERE token=?", (token,))}

    def hold(self, token, keys):
        self.db.executemany("INSERT OR IGNORE INTO held_removals VALUES (?,?)", ((token, k) for k in keys))

    def mature_removal(self, token, name, signature, now, delay):
        key = path_key(name)
        value = json.dumps(signature, sort_keys=True)
        row = self.db.execute("SELECT observed,signature FROM removals WHERE token=? AND path=?",
                              (token, key)).fetchone()
        if row and row[1] == value and row[0] <= now:
            return now - row[0] >= delay
        self.db.execute("INSERT OR REPLACE INTO removals VALUES (?,?,?,?)", (token, key, now, value))
        return False

    def reset_removals(self, token, observed, candidates, full):
        for key, in self.db.execute("SELECT path FROM removals WHERE token=?", (token,)).fetchall():
            if key not in candidates and (full or key in observed):
                self.db.execute("DELETE FROM removals WHERE token=? AND path=?", (token, key))

    def recent_deletions(self, token, now):
        self.db.execute("DELETE FROM recent_deletes WHERE token=? AND at<?", (token, now - 60))
        return self.db.execute("SELECT COUNT(*) FROM recent_deletes WHERE token=?", (token,)).fetchone()[0]


class LiveCopyRecords:
    """The mirror copy pipeline's ownership callbacks, scoped to a single live binding."""
    def __init__(self, store, token, target):
        self.store = store
        self.token = token
        self.target = target

    def _relative(self, path):
        name = os.path.relpath(canonical(path), self.target).replace("\\", "/")
        lexical_child_path(self.target, name)
        return name

    def register_temp(self, pair_id, path, identity):
        with self.store.db:
            self.store.db.execute("INSERT INTO stages VALUES(?,?,?)",
                                  (self.token, self._relative(path), json.dumps(identity)))

    def temps(self, pair_id):
        rows = self.store.db.execute("SELECT path,identity FROM stages WHERE token=?", (self.token,))
        return [(native(lexical_child_path(self.target, p)), tuple(json.loads(i))) for p, i in rows]

    def unregister_temp(self, path):
        with self.store.db:
            self.store.db.execute("DELETE FROM stages WHERE token=? AND path=?", (self.token, self._relative(path)))

    def finalize_copy(self, pair_id, name, source, target, digest, temp_path):
        with self.store.db:
            self.store.save(self.token, Agreed(name, name, source, target, digest))
            self.store.db.execute("DELETE FROM stages WHERE token=? AND path=?",
                                  (self.token, self._relative(temp_path)))
