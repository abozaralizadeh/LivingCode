"""Where new dependency versions come from.

LocalRegistry reads a local file and stands in for a package index. PypiRegistry is the
real adapter for later. Its URL is configurable, and tests point it at a local server.
"""
from __future__ import annotations

import json
import time
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Version:
    version: str
    published_at: float
    sha256: str


def version_key(version: str) -> tuple:
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


class Registry(ABC):
    @abstractmethod
    def versions(self, name: str) -> list[Version]:
        """All known versions of a package."""

    def newest_after_wait(self, name: str, current: str, wait_days: int) -> Version | None:
        """The newest version that's been public for long enough, in the same major version."""
        cutoff = time.time() - wait_days * 86400
        major = version_key(current)[0]
        options = [
            item for item in self.versions(name)
            if item.published_at <= cutoff
            and version_key(item.version)[0] == major
            and version_key(item.version) > version_key(current)
        ]
        return max(options, key=lambda item: version_key(item.version), default=None)

    def find(self, name: str, version: str) -> Version | None:
        return next((item for item in self.versions(name) if item.version == version), None)


class LocalRegistry(Registry):
    def __init__(self, path: str):
        self.path = Path(path)

    def versions(self, name):
        if not self.path.is_file():
            return []
        data = json.loads(self.path.read_text())
        return [Version(item["version"], item["published_at"], item["sha256"]) for item in data.get(name, [])]


class PypiRegistry(Registry):
    def __init__(self, base_url: str = "https://pypi.org"):
        self.base_url = base_url.rstrip("/")

    def versions(self, name):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{self.base_url}/pypi/{name}/json", timeout=20) as response:
            data = json.loads(response.read())
        found = []
        for version, files in data.get("releases", {}).items():
            wheels = [item for item in files if item.get("packagetype") == "bdist_wheel" and not item.get("yanked")]
            if wheels:
                stamp = datetime.fromisoformat(wheels[0]["upload_time_iso_8601"].replace("Z", "+00:00"))
                found.append(Version(version, stamp.timestamp(), wheels[0]["digests"]["sha256"]))
        return found


def make_registry(cfg) -> Registry:
    return LocalRegistry(cfg.sources.registry_file) if cfg.sources.registry_file else PypiRegistry()
