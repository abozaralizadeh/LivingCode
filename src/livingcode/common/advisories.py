"""Known security problems in dependencies.

LocalAdvisories reads a local file and stands in for a real advisory service.
PipAuditSource is the real adapter for later, tested with a fake command runner.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from . import runner as default_runner


@dataclass(frozen=True)
class Advisory:
    id: str
    package: str
    version: str
    fixed_version: str
    severity: str = "high"


class AdvisorySource(ABC):
    @abstractmethod
    def check(self, pins: dict) -> list[Advisory]:
        """Returns the advisories that affect these pinned versions."""


class LocalAdvisories(AdvisorySource):
    def __init__(self, path: str):
        self.path = Path(path)

    def check(self, pins: dict) -> list[Advisory]:
        if not self.path.is_file():
            return []
        found = []
        for item in json.loads(self.path.read_text()):
            pin = pins.get(item["package"].lower())
            if pin and pin.version in item["affected"]:
                found.append(Advisory(item["id"], pin.name, pin.version, item["fixed"], item.get("severity", "high")))
        return found


class PipAuditSource(AdvisorySource):
    def __init__(self, lock_path: str, runner=default_runner.run):
        self.lock_path = lock_path
        self.runner = runner

    def check(self, pins: dict) -> list[Advisory]:
        done = self.runner(["pip-audit", "-r", self.lock_path, "-f", "json"], timeout=120)
        try:
            data = json.loads(done.output)
        except ValueError as exc:
            raise RuntimeError(f"pip-audit gave no usable result: {done.output[:300]}") from exc
        found = []
        for dep in data.get("dependencies", []):
            for vuln in dep.get("vulns", []):
                fixes = vuln.get("fix_versions") or [""]
                found.append(Advisory(vuln["id"], dep["name"].lower(), dep["version"], fixes[0]))
        return found


def make_advisories(cfg, tree: Path | None = None) -> AdvisorySource:
    if cfg.sources.advisories_file:
        return LocalAdvisories(cfg.sources.advisories_file)
    return PipAuditSource(str((tree or cfg.current_link) / "requirements.lock"))
