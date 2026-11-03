"""The lab lock. For now, nothing here may touch anything except the throwaway sample app.

This is a plain hardcoded check that every part runs before it does anything. Changing
LAB_ONLY is a deliberate step for later, once the guard has been tested on Linux.
"""
from __future__ import annotations

import platform
import tempfile
from pathlib import Path
from urllib.parse import urlparse

LAB_ONLY = True
MARKER_FILE = "LIVINGCODE_THROWAWAY_SAMPLE"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class LabLockError(RuntimeError):
    pass


def is_loopback(url: str) -> bool:
    return urlparse(url).hostname in LOOPBACK


def _inside_allowed(path: Path) -> bool:
    roots = (PROJECT_ROOT, Path(tempfile.gettempdir()).resolve())
    return any(path == root or root in path.parents for root in roots)


def problems(cfg) -> list[str]:
    found = []
    if platform.system() != "Darwin":
        found.append("the lab lock only allows this Mac for now")
    if not (cfg.repo / MARKER_FILE).is_file():
        found.append(f"{cfg.repo} has no {MARKER_FILE} marker, so it isn't the sample app")
    for name, path in (
        ("app.repo", cfg.repo), ("app.root", cfg.app_root),
        ("paths.state_dir", cfg.state_dir), ("paths.log_dir", cfg.log_dir),
    ):
        if not _inside_allowed(path):
            found.append(f"{name} is outside the lab: {path}")
    urls = [cfg.health.url] + [check["url"] for check in cfg.health.smoke_checks]
    for url in urls:
        if not is_loopback(url):
            found.append(f"health checks must stay on this machine: {url}")
    if cfg.safety.alert_webhook and not is_loopback(cfg.safety.alert_webhook):
        found.append("the alert webhook must stay on this machine")
    if cfg.llm.provider != "fake" and not is_loopback(cfg.llm.base_url):
        found.append("real LLM hosts are refused, so no paid call can happen")
    if cfg.sync.push and cfg.sync.backend == "git":
        found.append("pushing to a real remote is refused")
    return found


def check(cfg) -> None:
    if not LAB_ONLY:
        return
    found = problems(cfg)
    if found:
        raise LabLockError("Refused by the lab lock: " + "; ".join(found))
