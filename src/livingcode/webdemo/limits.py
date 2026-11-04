"""Daily limits for the web demo: runs per visitor, runs in all, and AI tokens in all.

Counts are kept per UTC day in a small JSON file, so a restart doesn't reset them.
Visitor addresses are stored only as salted hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class Limits:
    def __init__(self, folder: Path, runs_per_ip: int, run_cap: int, token_budget: int, clock=today):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "limits.json"
        self.runs_per_ip = runs_per_ip
        self.run_cap = run_cap
        self.token_budget = token_budget
        self.clock = clock
        self.lock = threading.Lock()
        salt_file = self.folder / "salt"
        if not salt_file.exists():
            salt_file.write_text(secrets.token_hex(16))
            os.chmod(salt_file, 0o600)
        self.salt = salt_file.read_text().strip()
        self.data = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if data.get("day") != self.clock():
            data = {"day": self.clock(), "runs": 0, "tokens": 0, "visitors": {}}
        return data

    def _fresh(self) -> dict:
        if self.data.get("day") != self.clock():
            self.data = {"day": self.clock(), "runs": 0, "tokens": 0, "visitors": {}}
        return self.data

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)

    def _visitor(self, ip: str) -> str:
        return hashlib.sha256(f"{self.salt}:{self.data['day']}:{ip}".encode()).hexdigest()[:20]

    def visitor_id(self, ip: str) -> str:
        """A short salted hash for the logs, so raw addresses are never written down."""
        with self.lock:
            self._fresh()
            return self._visitor(ip)[:10]

    def tokens_left(self) -> int | None:
        """None means there's no daily token budget."""
        with self.lock:
            data = self._fresh()
            return None if not self.token_budget else max(0, self.token_budget - data["tokens"])

    def summary(self, ip: str) -> dict:
        with self.lock:
            data = self._fresh()
            used = data["visitors"].get(self._visitor(ip), 0)
            return {
                "runs_left_today": max(0, self.run_cap - data["runs"]),
                "your_runs_left": max(0, self.runs_per_ip - used),
                "budget_spent": bool(self.token_budget) and data["tokens"] >= self.token_budget,
            }

    def refusal(self, ip: str) -> str | None:
        """Why this visitor can't start a run now, in words for the page, or None."""
        with self.lock:
            data = self._fresh()
            if self.token_budget and data["tokens"] >= self.token_budget:
                return "Today's AI budget for this demo is used up. It resets at midnight UTC. You can still watch the last run."
            if data["runs"] >= self.run_cap:
                return "This demo has done all its runs for today. It resets at midnight UTC."
            if data["visitors"].get(self._visitor(ip), 0) >= self.runs_per_ip:
                return "You've used all your runs for today, thanks for trying it. Come back tomorrow."
            return None

    def record_run(self, ip: str) -> None:
        with self.lock:
            data = self._fresh()
            key = self._visitor(ip)
            data["runs"] += 1
            data["visitors"][key] = data["visitors"].get(key, 0) + 1
            self._save()

    def add_tokens(self, count: int) -> None:
        with self.lock:
            self._fresh()["tokens"] += max(0, int(count))
            self._save()
