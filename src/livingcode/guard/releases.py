"""Numbered release folders and the atomic switch of the current link."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")


def sha256_of(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class Releases:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg.releases_dir
        self.link = cfg.current_link

    def path(self, release_id: str) -> Path:
        return self.dir / release_id

    def current(self) -> str | None:
        return os.readlink(self.link).rsplit("/", 1)[-1] if self.link.is_symlink() else None

    def stage_path(self, name: str) -> Path:
        return self.dir / f".stage-{name}"

    def stage_from(self, source: Path, name: str) -> Path:
        stage = self.stage_path(name)
        shutil.rmtree(stage, ignore_errors=True)
        shutil.copytree(source, stage, ignore=SKIP, symlinks=True)
        return stage

    def finalize(self, stage: Path, release_id: str) -> Path:
        target = self.path(release_id)
        # A crash can leave a folder with this number that was never recorded.
        shutil.rmtree(target, ignore_errors=True)
        os.replace(stage, target)
        return target

    def switch(self, release_id: str) -> None:
        tmp = self.link.with_name(f".current-{os.getpid()}")
        tmp.unlink(missing_ok=True)
        os.symlink(self.path(release_id), tmp)
        os.replace(tmp, self.link)
        flags = os.open(self.link.parent, os.O_RDONLY)
        try:
            os.fsync(flags)
        finally:
            os.close(flags)

    def remove(self, release_id: str) -> None:
        shutil.rmtree(self.path(release_id), ignore_errors=True)

    def clean_stages(self) -> None:
        for item in self.dir.glob(".stage-*"):
            shutil.rmtree(item, ignore_errors=True)

    def prune(self, keep: int) -> None:
        current = self.current()
        names = sorted(p.name for p in self.dir.iterdir() if p.is_dir() and not p.name.startswith("."))
        for name in names[:-keep] if keep else names:
            if name != current:
                self.remove(name)


def apply_changes(stage: Path, changes) -> None:
    """Writes the changes into the staged copy. Paths are checked before this runs."""
    root = stage.resolve()
    for change in changes:
        target = stage / change.path
        if root not in target.parent.resolve().parents and target.parent.resolve() != root:
            raise ValueError(f"path escapes the release: {change.path}")
        if target.is_symlink():
            raise ValueError(f"won't write through a link: {change.path}")
        if change.action == "delete":
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(change.content)
