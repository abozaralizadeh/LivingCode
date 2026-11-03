"""Installs a release's dependencies into that release, so a rollback undoes them too.

LocalIndexInstaller is the local stand-in for a package index. PipInstaller is the real
one for later. It needs a network, so tests only check the commands it builds.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from . import lockfile, runner as default_runner
from .pathrules import LOCK_FILE


class InstallError(RuntimeError):
    pass


class Installer(ABC):
    @abstractmethod
    def install(self, tree: Path) -> None:
        """Installs everything in tree's lock file. Raises InstallError on any problem."""


def read_pins(tree: Path) -> dict:
    lock = tree / LOCK_FILE
    if not lock.exists():
        return {}
    try:
        return lockfile.parse(lock.read_text())
    except (lockfile.LockError, UnicodeDecodeError) as exc:
        raise InstallError(str(exc)) from exc


class LocalIndexInstaller(Installer):
    def __init__(self, index_dir: str):
        self.index = Path(index_dir)

    def source(self, name: str, version: str) -> Path:
        return self.index / name / version / f"{name}.py"

    def install(self, tree: Path) -> None:
        deps = tree / "_deps"
        shutil.rmtree(deps, ignore_errors=True)
        deps.mkdir()
        for pin in read_pins(tree).values():
            source = self.source(pin.name, pin.version)
            if not source.is_file():
                raise InstallError(f"{pin.name} {pin.version} isn't in the package index")
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != pin.sha256:
                raise InstallError(f"hash mismatch for {pin.name} {pin.version}")
            (deps / source.name).write_bytes(data)


class PipInstaller(Installer):
    def __init__(self, runner=default_runner.run):
        self.runner = runner

    def commands(self, tree: Path) -> list[list[str]]:
        venv = tree / "_deps"
        return [
            [sys.executable, "-m", "venv", str(venv)],
            [str(venv / "bin" / "python"), "-m", "pip", "install", "--no-input", "--require-hashes",
             "--only-binary", ":all:", "-r", str(tree / LOCK_FILE)],
        ]

    def install(self, tree: Path) -> None:
        read_pins(tree)
        for command in self.commands(tree):
            done = self.runner(command, timeout=300)
            if done.code != 0:
                raise InstallError(done.output[-2000:])


def make_installer(cfg, runner=default_runner.run) -> Installer:
    if cfg.sources.package_index:
        return LocalIndexInstaller(cfg.sources.package_index)
    return PipInstaller(runner)
