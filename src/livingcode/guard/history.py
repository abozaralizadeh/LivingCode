"""Keeps the history of what went live.

LocalHistory stores plain folders and stands in for git and a remote. GitHistory is the
real adapter for later, and tests only check the commands it builds.
"""
from __future__ import annotations

import json
import shlex
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

from ..common import runner as default_runner
from .releases import SKIP


class HistoryError(RuntimeError):
    pass


class History(ABC):
    @abstractmethod
    def record(self, release_id: str, tree: Path, message: str) -> str:
        """Stores a release and returns its reference."""

    @abstractmethod
    def push(self) -> None:
        """Shares settled releases with the remote."""

    @abstractmethod
    def export(self, ref: str, target: Path) -> None:
        """Writes the tree of a reference into target."""


class LocalHistory(History):
    def __init__(self, cfg):
        self.root = cfg.guard_dir / "history"
        self.remote = self.root / "remote"
        self.log = self.root / "log.jsonl"

    def record(self, release_id, tree, message):
        target = self.root / release_id
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(tree, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_deps"), symlinks=True)
        with open(self.log, "a") as handle:
            handle.write(json.dumps({"ref": release_id, "message": message, "time": time.time()}) + "\n")
        return release_id

    def push(self):
        self.remote.mkdir(parents=True, exist_ok=True)
        if self.log.exists():
            shutil.copy2(self.log, self.remote / "log.jsonl")

    def export(self, ref, target):
        source = self.root / ref
        if not source.is_dir() or "/" in ref:
            raise HistoryError(f"unknown reference: {ref}")
        shutil.copytree(source, target, ignore=SKIP, symlinks=True)


class GitHistory(History):
    def __init__(self, cfg, runner=default_runner.run):
        self.repo = str(cfg.repo)
        self.branch = cfg.sync.branch
        self.should_push = cfg.sync.push
        self.runner = runner

    def _git(self, *args, cwd=None):
        done = self.runner(["git", "-C", cwd or self.repo, *args], timeout=120)
        if done.code != 0:
            raise HistoryError(f"git {args[0]} failed: {done.output[-300:]}")
        return done.output.strip()

    def record(self, release_id, tree, message):
        self._git("--work-tree", str(tree), "add", "--all", "--", ".")
        self._git("commit", "--message", f"{release_id}: {message}")
        self._git("update-ref", f"refs/heads/{self.branch}", "HEAD")
        return self._git("rev-parse", "HEAD")

    def push(self):
        if self.should_push:
            self._git("push", "origin", f"refs/heads/{self.branch}")

    def export(self, ref, target):
        target.mkdir(parents=True)
        repo, name, folder = shlex.quote(self.repo), shlex.quote(ref), shlex.quote(str(target))
        command = f"git -C {repo} archive {name} | tar -x -C {folder}"
        done = self.runner(["sh", "-c", command], timeout=120)
        if done.code != 0:
            raise HistoryError(f"can't export {ref}: {done.output[-300:]}")


def make_history(cfg) -> History:
    return GitHistory(cfg) if cfg.sync.backend == "git" else LocalHistory(cfg)
