"""Reads a candidate handed over by the agent. Everything in it is treated as untrusted."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_CANDIDATE_BYTES = 5 * 1024 * 1024
ID_FORMAT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class CandidateError(ValueError):
    pass


@dataclass(frozen=True)
class Change:
    path: str
    action: str
    content: bytes | None
    base_sha256: str | None


@dataclass(frozen=True)
class Candidate:
    id: str
    folder: Path
    base_release: str
    capability: str
    issue_id: str
    summary: str
    feature_id: str
    changes: tuple
    changes_sha256: str


def _regular_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise CandidateError(f"{path.name} is missing or isn't a plain file")
    if path.stat().st_size > MAX_CANDIDATE_BYTES:
        raise CandidateError(f"{path.name} is too large")
    return path.read_bytes()


def load(folder: Path) -> Candidate:
    if folder.is_symlink() or not ID_FORMAT.match(folder.name):
        raise CandidateError("bad candidate folder name")
    try:
        manifest = json.loads(_regular_file(folder / "manifest.json"))
        raw = _regular_file(folder / "changes.json")
        items = json.loads(raw)
    except ValueError as exc:
        raise CandidateError(f"candidate isn't valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(items, list):
        raise CandidateError("candidate has the wrong shape")
    if manifest.get("id") != folder.name:
        raise CandidateError("manifest id doesn't match the folder")
    changes = []
    for item in items:
        try:
            action = item["action"]
            content = base64.b64decode(item["content"], validate=True) if action != "delete" else None
            if not isinstance(item["path"], str):
                raise TypeError("path must be text")
            changes.append(Change(item["path"], action, content, item.get("base_sha256")))
        except (KeyError, TypeError, ValueError) as exc:
            raise CandidateError(f"bad change entry: {exc}") from exc
        if action not in ("add", "modify", "delete"):
            raise CandidateError(f"unknown change action: {action}")
    return Candidate(
        id=folder.name, folder=folder, base_release=str(manifest.get("base_release", "")),
        capability=str(manifest.get("capability", "")), issue_id=str(manifest.get("issue_id", "")),
        summary=str(manifest.get("summary", ""))[:2000], feature_id=str(manifest.get("feature_id", "")),
        changes=tuple(changes), changes_sha256=hashlib.sha256(raw).hexdigest(),
    )
