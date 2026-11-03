"""Turns raw behaviour events into totals. Only these totals ever reach the LLM (see D14)."""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

EVENT_FIELDS = {"day", "route", "status", "ms", "session", "at"}


def purge_old(events_dir: Path, retention_days: int) -> int:
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in events_dir.glob("behaviour-*.jsonl*"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def read_events(events_dir: Path) -> list[dict]:
    events = []
    for path in sorted(events_dir.glob("behaviour-*.jsonl*")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and set(item) <= EVENT_FIELDS and "route" in item:
                events.append(item)
    return events


def totals(events: list[dict], min_count: int) -> dict:
    """Per-route totals. Routes with too few visits are left out, so nobody can be singled out."""
    visits, errors = Counter(), Counter()
    time_spent = defaultdict(float)
    by_session = {}
    for event in sorted(events, key=lambda item: item.get("at", 0)):
        route = str(event["route"])
        visits[route] += 1
        errors[route] += int(event.get("status", 200)) >= 500
        time_spent[route] += float(event.get("ms", 0))
        by_session[event.get("session")] = route
    exits = Counter(by_session.values())
    return {
        "sessions": len(by_session),
        "routes": [
            {
                "route": route,
                "visits": count,
                "error_rate": round(errors[route] / count, 3),
                "average_ms": round(time_spent[route] / count, 1),
                "left_from_here": exits[route],
            }
            for route, count in visits.most_common()
            if count >= min_count
        ],
    }
