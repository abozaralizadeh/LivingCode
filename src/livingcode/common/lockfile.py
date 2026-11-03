"""Reads and writes requirements.lock: one pinned, hashed dependency per line."""
from __future__ import annotations

import re
from dataclasses import dataclass

LINE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s+--hash=sha256:([0-9a-f]{64})$")


class LockError(ValueError):
    pass


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    sha256: str


def parse(text: str) -> dict[str, Pin]:
    pins = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.match(line)
        if not match:
            raise LockError(f"requirements.lock line {number} isn't a hashed pin")
        pin = Pin(match.group(1).lower(), match.group(2), match.group(3))
        pins[pin.name] = pin
    return pins


def render(pins: dict[str, Pin]) -> str:
    lines = [f"{p.name}=={p.version} --hash=sha256:{p.sha256}" for p in sorted(pins.values(), key=lambda p: p.name)]
    return "\n".join(lines) + "\n"
