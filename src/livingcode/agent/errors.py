"""Finds error blocks in logs and gives each kind of error a stable fingerprint."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
ERROR = re.compile(r"\b(ERROR|CRITICAL)\b")
EXCEPTION = re.compile(r"^(\w[\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b")
FRAME = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)')
MAX_BLOCK_LINES = 60


@dataclass(frozen=True)
class ErrorBlock:
    fingerprint: str
    title: str
    text: str


def fingerprint(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    kind = next((m.group(1) for line in reversed(lines) if (m := EXCEPTION.match(line.strip()))), "")
    frames = [m for line in lines if (m := FRAME.match(line))]
    place = f"{Path(frames[-1].group(1)).name}:{frames[-1].group(2)}" if frames else ""
    if not kind:
        first = re.sub(r"\d+", "N", STAMP.sub("", lines[0] if lines else ""))
        kind = re.sub(r"\s+", " ", first).strip()[:120]
    title = f"{kind} at {place}" if place else kind
    return hashlib.sha256(title.encode()).hexdigest()[:16], title


def parse_blocks(text: str) -> list[ErrorBlock]:
    blocks, current = [], None
    for line in text.splitlines():
        if STAMP.match(line):
            if current:
                blocks.append(current)
            current = [line] if ERROR.search(line) else None
        elif current is not None and len(current) < MAX_BLOCK_LINES:
            current.append(line)
    if current:
        blocks.append(current)
    return [ErrorBlock(*fingerprint("\n".join(block)), "\n".join(block)) for block in blocks]


def read_new(paths, offsets: dict) -> tuple[str, dict]:
    """Reads what's been added to each log file since the saved offsets."""
    parts, updated = [], dict(offsets)
    for name in paths:
        path = Path(name)
        if not path.is_file():
            continue
        size = path.stat().st_size
        start = offsets.get(str(path), 0)
        start = start if start <= size else 0
        with open(path, "rb") as handle:
            handle.seek(start)
            parts.append(handle.read().decode(errors="replace"))
        updated[str(path)] = size
    return "\n".join(parts), updated


def read_library_events(events_dir: Path, offsets: dict) -> tuple[list[ErrorBlock], dict]:
    text, updated = read_new([events_dir / "errors.jsonl"], offsets)
    blocks = []
    for line in text.splitlines():
        try:
            detail = json.loads(line)["text"]
        except (ValueError, KeyError, TypeError):
            continue
        blocks.append(ErrorBlock(*fingerprint(detail), detail))
    return blocks, updated
