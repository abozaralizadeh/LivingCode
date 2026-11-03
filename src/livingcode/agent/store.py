"""The agent's own database. The guard never reads this file."""
from __future__ import annotations

import json
import time

from ..common.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS issue(
    id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE, capability TEXT, title TEXT,
    detail TEXT, first_seen REAL, attempts INTEGER DEFAULT 0, status TEXT DEFAULT 'open',
    last_report TEXT);
CREATE TABLE IF NOT EXISTS trigger_event(
    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, time REAL, issue_id INTEGER, outcome TEXT);
CREATE TABLE IF NOT EXISTS round(
    id INTEGER PRIMARY KEY AUTOINCREMENT, trigger_id INTEGER, issue_id INTEGER, status TEXT,
    summary TEXT, started_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS llm_usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER, provider TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, estimated_cost REAL, time REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class AgentStore:
    def __init__(self, path):
        self.conn = connect(path, SCHEMA)

    def close(self):
        self.conn.close()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))

    def add_issue(self, fingerprint, capability, title, detail) -> tuple[int, bool]:
        row = self.conn.execute("SELECT id FROM issue WHERE fingerprint = ?", (fingerprint,)).fetchone()
        if row:
            return row["id"], False
        cursor = self.conn.execute(
            "INSERT INTO issue(fingerprint, capability, title, detail, first_seen) VALUES (?,?,?,?,?)",
            (fingerprint, capability, title, detail, time.time()),
        )
        return cursor.lastrowid, True

    def issue(self, issue_id):
        return self.conn.execute("SELECT * FROM issue WHERE id = ?", (issue_id,)).fetchone()

    def issues(self):
        return self.conn.execute("SELECT * FROM issue ORDER BY id").fetchall()

    def open_issues(self):
        return self.conn.execute("SELECT * FROM issue WHERE status = 'open' ORDER BY id").fetchall()

    def set_issue(self, issue_id, status=None, attempts=None, last_report=None):
        self.conn.execute(
            "UPDATE issue SET status = COALESCE(?, status), attempts = COALESCE(?, attempts),"
            " last_report = COALESCE(?, last_report) WHERE id = ?",
            (status, attempts, last_report, issue_id),
        )

    def add_trigger(self, kind, issue_id=None, outcome="") -> int:
        cursor = self.conn.execute(
            "INSERT INTO trigger_event(type, time, issue_id, outcome) VALUES (?,?,?,?)",
            (kind, time.time(), issue_id, outcome),
        )
        return cursor.lastrowid

    def set_trigger(self, trigger_id, outcome):
        self.conn.execute("UPDATE trigger_event SET outcome = ? WHERE id = ?", (outcome, trigger_id))

    def start_round(self, trigger_id, issue_id) -> int:
        cursor = self.conn.execute(
            "INSERT INTO round(trigger_id, issue_id, status, started_at) VALUES (?,?,?,?)",
            (trigger_id, issue_id, "running", time.time()),
        )
        return cursor.lastrowid

    def finish_round(self, round_id, status, summary):
        self.conn.execute(
            "UPDATE round SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
            (status, summary, time.time(), round_id),
        )

    def rounds_since(self, since: float) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM round WHERE started_at >= ?", (since,)).fetchone()["n"]

    def rounds(self):
        return self.conn.execute("SELECT * FROM round ORDER BY id DESC").fetchall()

    def add_usage(self, round_id, provider, model, input_tokens, output_tokens, cost):
        self.conn.execute(
            "INSERT INTO llm_usage(round_id, provider, model, input_tokens, output_tokens, estimated_cost, time)"
            " VALUES (?,?,?,?,?,?,?)",
            (round_id, provider, model, input_tokens, output_tokens, cost, time.time()),
        )

    def spent_in_round(self, round_id) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0) AS s FROM llm_usage WHERE round_id = ?", (round_id,)
        ).fetchone()
        return row["s"]

    def spent_since(self, since: float) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0) AS s FROM llm_usage WHERE time >= ?", (since,)
        ).fetchone()
        return row["s"]
