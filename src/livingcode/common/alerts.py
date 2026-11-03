"""Alerts for the operator. A failed alert must never stop or slow a rollback."""
from __future__ import annotations

import json
import time
import urllib.request

from .logs import append_limited


def send(cfg, level: str, title: str, detail: str = "") -> None:
    entry = {"time": time.time(), "level": level, "title": title, "detail": detail, "app": cfg.app.name}
    try:
        append_limited(cfg.log_dir / "alerts.log", json.dumps(entry))
    except OSError:
        pass
    url = cfg.safety.alert_webhook
    if not url:
        return
    try:
        request = urllib.request.Request(
            url, data=json.dumps(entry).encode(), headers={"Content-Type": "application/json"}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(request, timeout=3).close()
    except Exception:
        pass
