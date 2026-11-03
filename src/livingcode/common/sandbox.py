"""Throwaway sandbox for running project code: no network, no secrets, a temporary copy.

If no real sandbox exists on a machine, this refuses to run instead of running unprotected.
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from . import runner as default_runner

OUTPUT_LIMIT = 20000


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class SandboxResult:
    code: int
    output: str
    timed_out: bool = False


def _env(tree: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tree),
        "TMPDIR": str(tree / ".tmp"),
        "PYTHONPATH": str(tree / "_deps"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C.UTF-8",
    }


class Sandbox(ABC):
    @abstractmethod
    def run(self, tree: Path, command: list[str], timeout: float) -> SandboxResult:
        """Runs a command in a temporary copy of tree."""


class MacSandbox(Sandbox):
    def __init__(self, deny_read: tuple = ()):
        self.deny_read = tuple(str(Path(p)) for p in deny_read)

    def _profile(self, workdir: Path) -> str:
        allow_read = [str(workdir), sys.prefix, sys.base_prefix]
        lines = ["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)"]
        for path in (Path.home(), *self.deny_read):
            lines.append(f'(deny file-read* (subpath "{path}"))')
        for path in allow_read:
            lines.append(f'(allow file-read* (subpath "{Path(path).resolve()}"))')
        lines.append(f'(allow file-write* (subpath "{workdir}"))')
        lines.append('(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper"))')
        return "\n".join(lines)

    def run(self, tree: Path, command: list[str], timeout: float) -> SandboxResult:
        if not shutil.which("sandbox-exec"):
            raise SandboxUnavailable("sandbox-exec isn't available")
        base = Path(tempfile.mkdtemp(prefix="lc-sandbox-")).resolve()
        workdir = base / "tree"
        try:
            shutil.copytree(tree, workdir, symlinks=False)
            (workdir / ".tmp").mkdir(exist_ok=True)
            wrapped = ["sandbox-exec", "-p", self._profile(base), *command]
            process = subprocess.Popen(
                wrapped, cwd=workdir, env=_env(workdir), text=True, errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                output, _ = process.communicate(timeout=timeout)
                return SandboxResult(process.returncode, output[-OUTPUT_LIMIT:])
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
                return SandboxResult(124, (output or "")[-OUTPUT_LIMIT:], timed_out=True)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class SystemdSandbox(Sandbox):
    """Linux version. Not run on this Mac, so tests only check the command it builds."""

    def __init__(self, runner=default_runner.run):
        self.runner = runner

    def command(self, workdir: Path, command: list[str], timeout: float) -> list[str]:
        properties = (
            "DynamicUser=yes", "PrivateNetwork=yes", "ProtectSystem=strict", "ProtectHome=yes",
            "PrivateTmp=yes", "NoNewPrivileges=yes", f"RuntimeMaxSec={int(timeout)}",
            f"ReadWritePaths={workdir}", "MemoryMax=1G", "CPUQuota=100%",
        )
        env = [f"--setenv={key}={value}" for key, value in _env(workdir).items()]
        props = [f"--property={item}" for item in properties]
        return ["systemd-run", "--wait", "--pipe", "--collect", f"--working-directory={workdir}",
                *props, *env, *command]

    def run(self, tree: Path, command: list[str], timeout: float) -> SandboxResult:
        base = Path(tempfile.mkdtemp(prefix="lc-sandbox-"))
        workdir = base / "tree"
        try:
            shutil.copytree(tree, workdir, symlinks=False)
            (workdir / ".tmp").mkdir(exist_ok=True)
            os.chmod(base, 0o755)
            done = self.runner(self.command(workdir, command, timeout), timeout=timeout + 5)
            return SandboxResult(done.code, done.output[-OUTPUT_LIMIT:], timed_out=done.code == 124)
        finally:
            shutil.rmtree(base, ignore_errors=True)


def make_sandbox(cfg) -> Sandbox:
    system = platform.system()
    if system == "Darwin":
        return MacSandbox(deny_read=(cfg.state_dir, cfg.log_dir))
    if system == "Linux" and shutil.which("systemd-run"):
        return SystemdSandbox()
    raise SandboxUnavailable(f"no sandbox available on {system}")
