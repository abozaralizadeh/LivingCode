"""Starts and stops the app. Local mode for this Mac, systemd mode for Linux later."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

from ..common import runner as default_runner
from ..common.logs import MAX_BYTES


class ProcessManager(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_active(self) -> bool: ...

    def restart(self) -> None:
        self.stop()
        self.start()


class SystemdManager(ProcessManager):
    """Not run on this Mac. Tests only check the commands it builds."""

    def __init__(self, cfg, runner=default_runner.run):
        self.service = cfg.app.service
        self.runner = runner

    def start(self):
        self._must(["sudo", "-n", "systemctl", "start", self.service])

    def stop(self):
        self._must(["sudo", "-n", "systemctl", "stop", self.service])

    def restart(self):
        self._must(["sudo", "-n", "systemctl", "restart", self.service])

    def is_active(self):
        return self.runner(["systemctl", "is-active", "--quiet", self.service], timeout=10).code == 0

    def _must(self, command):
        done = self.runner(command, timeout=60)
        if done.code != 0:
            raise RuntimeError(f"{' '.join(command)} failed: {done.output[-300:]}")


class LocalProcessManager(ProcessManager):
    """The app holds a lock on app.lock for as long as it runs.

    That lock is how the guard knows the app is alive. It needs no other program, no network
    and no help from the app, and a reused process id can never look like the app. Signals
    are only ever sent while the lock shows the app's process group is still there.
    """

    STOP_GRACE = 1.5
    KILL_WAIT = 5.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.pidfile = cfg.guard_dir / "app.pid"
        self.lockfile = cfg.guard_dir / "app.lock"
        self.children = []

    def _open_lock(self) -> int:
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        return os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o600)

    def _reap(self) -> None:
        self.children = [child for child in self.children if child.poll() is None]

    def _pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def is_active(self) -> bool:
        self._reap()
        try:
            fd = self._open_lock()
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def start(self) -> None:
        current = str(self.cfg.current_link)
        command = [
            part.replace("{python}", sys.executable).replace("{current}", current)
            for part in self.cfg.app.start_command
        ]
        env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": f"{current}/_deps:{Path(__file__).resolve().parents[2]}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LIVINGCODE_CONFIG": str(self.cfg.path),
            "DATA_DIR": str(self.cfg.shared_dir),
            **self.cfg.app.env,
        }
        output_path = self.cfg.log_dir / f"{self.cfg.app.name}-stdout.log"
        if output_path.exists() and output_path.stat().st_size > MAX_BYTES:
            output_path.unlink()
        fd = self._open_lock()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("the app is still running, so it can't be started again") from None
            with open(output_path, "ab") as output:
                # The child keeps its own copy of the locked file, and so keeps the lock.
                process = subprocess.Popen(
                    command, cwd=current, env=env, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(fd,),
                )
        finally:
            os.close(fd)
        self.children.append(process)
        self.pidfile.write_text(str(process.pid))

    def stop(self) -> None:
        if not self.is_active():
            self.pidfile.unlink(missing_ok=True)
            return
        pid = self._pid()
        if pid is None:
            raise RuntimeError("the app is running but its process id is unknown, so it can't be stopped")
        self._signal(pid, signal.SIGTERM)
        if not self._wait_until_stopped(self.STOP_GRACE):
            self._signal(pid, signal.SIGKILL)
            if not self._wait_until_stopped(self.KILL_WAIT):
                raise RuntimeError("the app didn't stop, even after SIGKILL")
        self.pidfile.unlink(missing_ok=True)

    def _wait_until_stopped(self, seconds: float) -> bool:
        deadline = time.time() + seconds
        while self.is_active():
            if time.time() > deadline:
                return False
            time.sleep(0.02)
        return True

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass


def make_process_manager(cfg) -> ProcessManager:
    return SystemdManager(cfg) if cfg.app.runner == "systemd" else LocalProcessManager(cfg)
