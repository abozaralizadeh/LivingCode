"""Fixed health checks. They give the same answer every time and use no judgement."""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ERROR_LINE = re.compile(r"\b(ERROR|CRITICAL)\b")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
LOOKBACK_BYTES = 2 * 1024 * 1024


CHECK_AGENT = "livingcode-guard"


def fetch(url: str, timeout: float = 2.0) -> tuple[int | None, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": CHECK_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(65536).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return None, ""


def smoke_failure(cfg) -> str | None:
    for check in cfg.health.smoke_checks:
        status, body = fetch(check["url"])
        if status != check.get("expect_status", 200):
            return f"smoke check {check['url']} gave {status}"
        text = check.get("expect_text", "")
        if text and text not in body:
            return f"smoke check {check['url']} didn't contain the expected text"
    return None


def startup_failure(cfg, pm) -> str | None:
    """Checks the service comes up, then that health answers OK enough times in a row."""
    health = cfg.health
    deadline = time.time() + health.startup_seconds
    while not pm.is_active():
        if time.time() > deadline:
            return "service didn't reach active in time"
        time.sleep(0.05)
    good = 0
    deadline = time.time() + health.window_seconds
    while time.time() <= deadline:
        if not pm.is_active():
            return "service died during startup"
        status, _ = fetch(health.url)
        good = good + 1 if status == 200 else 0
        if good >= health.ok_count:
            return smoke_failure(cfg)
        time.sleep(health.interval_seconds)
    return f"health URL didn't answer OK {health.ok_count} times in a row"


class ErrorWatch:
    """Counts new error lines in the app's logs, starting from when it's created."""

    def __init__(self, cfg):
        self.files = [Path(item) for item in cfg.app.log_files]
        self.offsets = {path: self._size(path) for path in self.files}
        self.started = time.time()
        self.seen = 0

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def new_errors(self) -> int:
        for path in self.files:
            size = self._size(path)
            start = self.offsets[path] if size >= self.offsets[path] else 0
            if size > start:
                with open(path, "rb") as handle:
                    handle.seek(start)
                    text = handle.read().decode(errors="replace")
                self.seen += sum(1 for line in text.splitlines() if ERROR_LINE.search(line))
            self.offsets[path] = size
        return self.seen


def normal_error_rate(cfg) -> float:
    """Errors per minute over the last 24 hours of whatever log is still available."""
    since = time.time() - 86400
    count = 0
    for name in cfg.app.log_files:
        path = Path(name)
        if not path.is_file():
            continue
        with open(path, "rb") as handle:
            handle.seek(max(0, path.stat().st_size - LOOKBACK_BYTES))
            text = handle.read().decode(errors="replace")
        for line in text.splitlines():
            match = STAMP.match(line)
            if match and ERROR_LINE.search(line):
                stamp = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc)
                count += stamp.timestamp() >= since
    return count / 1440


def probation_failure(cfg, pm, watch: ErrorWatch, normal_rate: float, tick=None) -> str | None:
    health = cfg.health
    deadline = time.time() + health.probation_minutes * 60
    while True:
        if not pm.is_active():
            return "service stopped during probation"
        status, _ = fetch(health.url)
        if status != 200:
            return f"health URL gave {status} during probation"
        failure = smoke_failure(cfg)
        if failure:
            return failure
        errors = watch.new_errors()
        minutes = max((time.time() - watch.started) / 60, 1.0)
        if errors >= health.min_errors and errors / minutes > health.error_rate_factor * normal_rate:
            return f"{errors} errors during probation, above the normal rate"
        if tick:
            tick()
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        time.sleep(min(health.poll_seconds, remaining))
