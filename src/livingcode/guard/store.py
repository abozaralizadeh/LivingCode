"""The guard's own database. The agent never reads or writes this file."""
from __future__ import annotations

import json
import time

from ..common.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS release(
    id TEXT PRIMARY KEY, commit_ref TEXT, source TEXT, state TEXT,
    created_at REAL, settled_at REAL);
CREATE TABLE IF NOT EXISTS deployment(
    id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT, release_id TEXT,
    previous_release TEXT, source TEXT, capability TEXT, issue_id TEXT, state TEXT,
    failed_check TEXT, started_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS backup(
    id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT, path TEXT, size INTEGER,
    data_included INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS breaker(
    id INTEGER PRIMARY KEY CHECK(id = 1), state TEXT, opened_at REAL, reason TEXT,
    consecutive_rollbacks INTEGER);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, actor TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit log rows are only ever added'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit log rows are only ever added'); END;
INSERT OR IGNORE INTO breaker(id, state, consecutive_rollbacks) VALUES (1, 'closed', 0);
"""

OPEN_STATES = ("Checking", "BackingUp", "Building", "Switching", "Probation", "RollingBack")


class GuardStore:
    def __init__(self, path):
        self.conn = connect(path, SCHEMA)

    def close(self):
        self.conn.close()

    def audit(self, actor, action, detail=""):
        self.conn.execute(
            "INSERT INTO audit_log(time, actor, action, detail) VALUES (?,?,?,?)",
            (time.time(), actor, action, detail if isinstance(detail, str) else json.dumps(detail)),
        )

    def audit_rows(self, limit=200):
        return self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def next_release_id(self) -> str:
        row = self.conn.execute("SELECT id FROM release ORDER BY id DESC LIMIT 1").fetchone()
        number = int(row["id"][1:]) + 1 if row else 1
        return f"r{number:04d}"

    def add_release(self, release_id, source, state="built", commit_ref=""):
        self.conn.execute(
            "INSERT INTO release(id, commit_ref, source, state, created_at) VALUES (?,?,?,?,?)",
            (release_id, commit_ref, source, state, time.time()),
        )

    def set_release_state(self, release_id, state):
        settled = time.time() if state == "settled" else None
        self.conn.execute(
            "UPDATE release SET state = ?, settled_at = COALESCE(?, settled_at) WHERE id = ?",
            (state, settled, release_id),
        )

    def set_commit(self, release_id, commit_ref):
        self.conn.execute("UPDATE release SET commit_ref = ? WHERE id = ?", (commit_ref, release_id))

    def release(self, release_id):
        return self.conn.execute("SELECT * FROM release WHERE id = ?", (release_id,)).fetchone()

    def releases(self):
        return self.conn.execute("SELECT * FROM release ORDER BY id DESC").fetchall()

    def new_deployment(self, candidate_id, previous, source, capability="", issue_id="") -> int:
        cursor = self.conn.execute(
            "INSERT INTO deployment(candidate_id, previous_release, source, capability, issue_id, state, started_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (candidate_id, previous, source, capability, issue_id, "Checking", time.time()),
        )
        return cursor.lastrowid

    def set_deployment(self, deployment_id, state, release_id=None, failed_check=None, finished=False):
        self.conn.execute(
            "UPDATE deployment SET state = ?, release_id = COALESCE(?, release_id),"
            " failed_check = COALESCE(?, failed_check), finished_at = ? WHERE id = ?",
            (state, release_id, failed_check, time.time() if finished else None, deployment_id),
        )

    def deployment(self, deployment_id):
        return self.conn.execute("SELECT * FROM deployment WHERE id = ?", (deployment_id,)).fetchone()

    def deployments(self, limit=50):
        return self.conn.execute("SELECT * FROM deployment ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def open_deployment(self):
        marks = ",".join("?" for _ in OPEN_STATES)
        return self.conn.execute(
            f"SELECT * FROM deployment WHERE state IN ({marks}) ORDER BY id DESC LIMIT 1", OPEN_STATES
        ).fetchone()

    def agent_deploys_since(self, since: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM deployment WHERE source = 'agent' AND started_at >= ?"
            " AND state IN ('Settled', 'RolledBack', 'NeedsHuman', 'Switching', 'Probation', 'RollingBack')",
            (since,),
        ).fetchone()
        return row["n"]

    def last_agent_rollback(self):
        row = self.conn.execute(
            "SELECT MAX(finished_at) AS at FROM deployment WHERE source = 'agent'"
            " AND state IN ('RolledBack', 'NeedsHuman')"
        ).fetchone()
        return row["at"]

    def clean_agent_releases(self) -> int:
        """Agent releases that settled since the agent's last rollback."""
        last = self.last_agent_rollback() or 0
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM deployment WHERE source = 'agent' AND state = 'Settled'"
            " AND finished_at > ?", (last,),
        ).fetchone()
        return row["n"]

    def add_backup(self, release_id, path, size, data_included):
        self.conn.execute(
            "INSERT INTO backup(release_id, path, size, data_included, created_at) VALUES (?,?,?,?,?)",
            (release_id, path, size, int(data_included), time.time()),
        )

    def backups(self):
        return self.conn.execute("SELECT * FROM backup ORDER BY id DESC").fetchall()

    def delete_backup(self, backup_id):
        self.conn.execute("DELETE FROM backup WHERE id = ?", (backup_id,))

    def breaker(self):
        return self.conn.execute("SELECT * FROM breaker WHERE id = 1").fetchone()

    def open_breaker(self, reason):
        self.conn.execute(
            "UPDATE breaker SET state = 'open', opened_at = ?, reason = ? WHERE id = 1", (time.time(), reason)
        )

    def close_breaker(self):
        self.conn.execute(
            "UPDATE breaker SET state = 'closed', opened_at = NULL, reason = NULL, consecutive_rollbacks = 0"
            " WHERE id = 1"
        )

    def count_rollback(self) -> int:
        self.conn.execute("UPDATE breaker SET consecutive_rollbacks = consecutive_rollbacks + 1 WHERE id = 1")
        return self.breaker()["consecutive_rollbacks"]

    def reset_rollback_count(self):
        self.conn.execute("UPDATE breaker SET consecutive_rollbacks = 0 WHERE id = 1")

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
