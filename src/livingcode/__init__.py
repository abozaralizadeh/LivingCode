"""The part that runs inside your app. With the flag off it does nothing at all, and
nothing here is allowed to raise an error into your app."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path

__version__ = "0.4.0"
_MAX_EVENT_FILE = 2 * 1024 * 1024
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F-]{8,})$")

_state = {"events_dir": None, "behaviour": False, "salt": "", "salt_day": ""}
_lock = threading.Lock()


def _write(path: Path, entry: dict) -> None:
    from .common.logs import append_limited

    append_limited(path, json.dumps(entry), max_bytes=_MAX_EVENT_FILE)


class _ErrorEvents(logging.Handler):
    def emit(self, record):
        try:
            if record.name.startswith("livingcode"):
                return
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            record_error(text)
        except Exception:
            pass


def start(config_path: str | None = None) -> bool:
    """Reads the feature flag. Returns True only if the flag is on and setup worked."""
    try:
        path = config_path or os.environ.get("LIVINGCODE_CONFIG")
        if not path:
            return False
        from .common import config

        cfg = config.load(path)
        if not cfg.enabled:
            return False
        _state["events_dir"] = cfg.events_dir
        _state["behaviour"] = cfg.mode("watching_user_behaviour") != "off"
        logging.getLogger().addHandler(_ErrorEvents(level=logging.ERROR))
        previous = sys.excepthook

        def hook(kind, value, trace):
            record_error("".join(traceback.format_exception(kind, value, trace)))
            previous(kind, value, trace)

        sys.excepthook = hook
        return True
    except Exception:
        return False


def record_error(text: str) -> None:
    """Writes an error event for the agent to pick up."""
    try:
        if _state["events_dir"] is not None:
            _write(_state["events_dir"] / "errors.jsonl", {"at": time.time(), "text": str(text)[:8000]})
    except Exception:
        pass


def normalize_route(path: str) -> str:
    """Drops the query and replaces ids, so a route can't point at one person."""
    parts = str(path).split("?", 1)[0].split("/")
    return "/".join(":id" if _ID_SEGMENT.match(part) else part for part in parts)[:200]


def _daily_salt(day: str) -> str:
    with _lock:
        if _state["salt_day"] != day:
            _state["salt"] = secrets.token_hex(16)
            _state["salt_day"] = day
        return _state["salt"]


def record_request(path: str, status: int, duration_ms: float, session_hint: str = "", user_agent: str = "") -> None:
    """Records one request. It takes no address, no body and no account details, on purpose.

    session_hint is hashed with a random value that's thrown away each day, so the session
    value can't be linked across days or back to the hint. The guard's own health checks
    say who they are in user_agent, and aren't counted as visits.
    """
    try:
        if not _state["behaviour"] or _state["events_dir"] is None:
            return
        if str(user_agent).startswith("livingcode-guard"):
            return
        day = time.strftime("%Y%m%d", time.gmtime())
        seed = session_hint or secrets.token_hex(8)
        session = hashlib.sha256((_daily_salt(day) + seed).encode()).hexdigest()[:16]
        entry = {
            "day": day, "route": normalize_route(path), "status": int(status),
            "ms": round(float(duration_ms), 1), "session": session, "at": time.time(),
        }
        _write(_state["events_dir"] / f"behaviour-{day}.jsonl", entry)
    except Exception:
        pass
