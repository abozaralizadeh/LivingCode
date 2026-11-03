"""Runs outside commands. Adapters take this as an argument, so tests can pass a fake
one and check the commands without ever running them."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class Result:
    code: int
    output: str


def run(command: list[str], cwd=None, timeout: float = 120, env=None) -> Result:
    try:
        done = subprocess.run(
            command, cwd=cwd, env=env, timeout=timeout, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        return Result(124, f"timed out after {timeout} seconds: {command[0]}")
    except OSError as exc:
        return Result(127, f"can't run {command[0]}: {exc}")
    return Result(done.returncode, done.stdout)
