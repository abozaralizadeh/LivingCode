"""The guard's main loop. It starts with the system and doesn't depend on the agent."""
from __future__ import annotations

import time
from pathlib import Path

from ..common import config as config_module
from ..common import alerts
from .deploy import Deployer


def waiting_candidates(cfg) -> list[Path]:
    folders = [p for p in cfg.candidates_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return sorted(folders, key=lambda p: p.stat().st_mtime)


def tick(deployer: Deployer) -> int:
    handled = 0
    for folder in waiting_candidates(deployer.cfg):
        outcome = deployer.process_candidate(folder)
        deployer.log.info("candidate %s: %s %s", folder.name, outcome.state, outcome.detail[:200])
        handled += 1
    return handled


def run_forever(config_path, poll_seconds: float = 2.0) -> None:
    cfg = config_module.load(config_path)
    deployer = Deployer(cfg)
    deployer.recover()
    while True:
        try:
            deployer.cfg = config_module.load(config_path)
        except config_module.ConfigError as exc:
            alerts.send(deployer.cfg, "warning", "Config can't be read, keeping the last good one", str(exc))
        try:
            tick(deployer)
        except Exception:
            deployer.log.exception("guard tick failed")
        time.sleep(poll_seconds)
