"""The deploy and rollback state machine. Every step is saved first, so a crash can be
picked up again. A rollback needs no network, no LLM and no working app."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common import alerts, lab
from ..common.advisories import make_advisories
from ..common.installer import InstallError, make_installer
from ..common.logs import get_logger
from ..common.sandbox import make_sandbox
from . import backup, candidates, checks, health
from .history import make_history
from .procman import make_process_manager
from .releases import Releases
from .scan import BanditScanner, BuiltinScanner
from .store import GuardStore

LOG_EXCERPT_BYTES = 4000


@dataclass
class Outcome:
    state: str
    deployment_id: int
    release_id: str | None = None
    failed_check: str | None = None
    detail: str = ""


class Deployer:
    def __init__(self, cfg, pm=None, sandbox=None, installer=None, history=None, advisories=None, hook=None):
        lab.check(cfg)
        self.cfg = cfg
        for folder in cfg.all_dirs():
            folder.mkdir(parents=True, exist_ok=True)
        self.store = GuardStore(cfg.guard_dir / "guard.db")
        self.releases = Releases(cfg)
        self.pm = pm or make_process_manager(cfg)
        self.sandbox = sandbox or make_sandbox(cfg)
        self.installer = installer or make_installer(cfg)
        self.history = history or make_history(cfg)
        self.advisories = advisories or make_advisories(cfg)
        self.scanners = []
        if "builtin" in cfg.safety.scanners:
            self.scanners.append(BuiltinScanner())
        if "bandit" in cfg.safety.scanners:
            self.scanners.append(BanditScanner())
        self.hook = hook or (lambda step: None)
        self.log = get_logger(cfg.log_dir, "guard")

    @contextmanager
    def _exclusive(self):
        with open(self.cfg.guard_dir / "deploy.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _state(self, deployment_id, state, **fields):
        self.store.set_deployment(deployment_id, state, **fields)
        self.log.info("deployment %s -> %s", deployment_id, state)
        self.hook(state)

    def _report(self, deployment_id, outcome, failed_check, detail, did, counts_as_attempt=True):
        row = self.store.deployment(deployment_id)
        report = {
            "deployment_id": deployment_id, "candidate_id": row["candidate_id"],
            "issue_id": row["issue_id"], "capability": row["capability"], "source": row["source"],
            "outcome": outcome, "failed_check": failed_check, "detail": detail[:4000],
            "log_excerpt": self._log_excerpt(), "guard_did": did,
            "counts_as_attempt": counts_as_attempt, "time": time.time(),
        }
        tmp = self.cfg.reports_dir / f".{deployment_id}.tmp"
        tmp.write_text(json.dumps(report, indent=2))
        os.replace(tmp, self.cfg.reports_dir / f"{deployment_id:05d}.json")

    def _log_excerpt(self) -> str:
        parts = []
        for name in self.cfg.app.log_files:
            path = Path(name)
            if path.is_file():
                with open(path, "rb") as handle:
                    handle.seek(max(0, path.stat().st_size - LOG_EXCERPT_BYTES))
                    parts.append(handle.read().decode(errors="replace"))
        return "\n".join(parts)[-LOG_EXCERPT_BYTES:]

    def _archive_candidate(self, folder: Path):
        done = self.cfg.guard_dir / "processed"
        done.mkdir(exist_ok=True)
        shutil.rmtree(done / folder.name, ignore_errors=True)
        try:
            os.replace(folder, done / folder.name)
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)

    def bootstrap(self) -> str:
        """Creates the first release from the repo, and starts the app."""
        with self._exclusive():
            if self.releases.current():
                if not self.pm.is_active():
                    self.pm.start()
                return self.releases.current()
            release_id = self.store.next_release_id()
            stage = self.releases.stage_from(self.cfg.repo, "bootstrap")
            self.installer.install(stage)
            self.releases.finalize(stage, release_id)
            self.store.add_release(release_id, "human", "settled")
            self.store.set_release_state(release_id, "settled")
            self.releases.switch(release_id)
            self.pm.restart()
            failure = health.startup_failure(self.cfg, self.pm)
            if failure:
                raise RuntimeError(f"first release failed: {failure}")
            self.store.audit("guard", "bootstrap", release_id)
            return release_id

    def process_candidate(self, folder: Path) -> Outcome:
        with self._exclusive():
            return self._process_candidate(folder)

    def _process_candidate(self, folder: Path) -> Outcome:
        previous = self.releases.current()
        try:
            if not previous:
                raise candidates.CandidateError("there's no live release yet")
            candidate = candidates.load(folder)
        except candidates.CandidateError as exc:
            deployment_id = self.store.new_deployment(folder.name[:64], previous, "agent")
            self.hook("Checking")
            return self._reject(deployment_id, folder, checks.Failure("format", str(exc)))
        deployment_id = self.store.new_deployment(
            candidate.id, previous, "agent", candidate.capability, candidate.issue_id
        )
        self.hook("Checking")
        base = self.releases.path(previous)
        stage = self.releases.stage_from(base, str(deployment_id))
        failure = checks.run_all(self, candidate, base, stage)
        if failure:
            shutil.rmtree(stage, ignore_errors=True)
            return self._reject(deployment_id, folder, failure)
        outcome = self._roll_out(deployment_id, stage, previous, "agent", candidate.summary)
        self._archive_candidate(folder)
        return outcome

    def _reject(self, deployment_id, folder, failure) -> Outcome:
        self._state(deployment_id, "Rejected", failed_check=failure.check, finished=True)
        self._report(deployment_id, "Rejected", failure.check, failure.detail,
                     "refused the candidate, users never saw it", failure.counts_as_attempt)
        self.store.audit("guard", "rejected", {"deployment": deployment_id, "check": failure.check})
        self._archive_candidate(folder)
        return Outcome("Rejected", deployment_id, None, failure.check, failure.detail)

    def deploy_ref(self, ref: str) -> Outcome:
        """A person's change: same backup, switch, health and rollback, without the agent limits."""
        with self._exclusive():
            previous = self.releases.current()
            deployment_id = self.store.new_deployment(f"human-{ref}"[:64], previous, "human")
            self.hook("Checking")
            stage = self.releases.stage_path(str(deployment_id))
            shutil.rmtree(stage, ignore_errors=True)
            source = Path(ref)
            if source.is_dir():
                self.releases.stage_from(source, str(deployment_id))
            else:
                self.history.export(ref, stage)
            return self._roll_out(deployment_id, stage, previous, "human", f"deploy of {ref}")

    def _roll_out(self, deployment_id, stage, previous, source, message) -> Outcome:
        watch_ready = False
        try:
            self._state(deployment_id, "BackingUp")
            if previous:
                backup.make(self.cfg, self.store, previous, deployment_id, self.releases.path(previous))
            self._state(deployment_id, "Building")
            if not (stage / "_deps").is_dir():
                self.installer.install(stage)
        except (backup.BackupError, InstallError, OSError) as exc:
            shutil.rmtree(stage, ignore_errors=True)
            return self._stop(deployment_id, "backup_or_build", str(exc))
        release_id = self.store.next_release_id()
        self.releases.finalize(stage, release_id)
        self.store.add_release(release_id, source)
        watch = health.ErrorWatch(self.cfg)
        normal_rate = health.normal_error_rate(self.cfg)
        try:
            self._state(deployment_id, "Switching", release_id=release_id)
            self.releases.switch(release_id)
            self.hook("Switched")
            self.pm.restart()
            self.hook("Restarted")
            failure = health.startup_failure(self.cfg, self.pm)
            if not failure:
                self._state(deployment_id, "Probation")
                watch_ready = True
                failure = health.probation_failure(
                    self.cfg, self.pm, watch, normal_rate, tick=lambda: self.hook("ProbationTick")
                )
        except Exception as exc:
            self.log.exception("deploy step failed")
            failure = f"deploy step failed: {exc}"
        if failure:
            check = "probation" if watch_ready else "startup"
            return self._rollback(deployment_id, previous, release_id, check, failure)
        return self._settle(deployment_id, release_id, message)

    def _stop(self, deployment_id, check, detail) -> Outcome:
        self._state(deployment_id, "Stopped", failed_check=check, finished=True)
        self._report(deployment_id, "Stopped", check, detail, "stopped before the switch, nothing changed")
        alerts.send(self.cfg, "warning", "Deploy stopped before the switch", detail[:500])
        self.store.audit("guard", "stopped", {"deployment": deployment_id, "check": check})
        return Outcome("Stopped", deployment_id, None, check, detail)

    def _settle(self, deployment_id, release_id, message) -> Outcome:
        self.store.set_release_state(release_id, "settled")
        if self.store.deployment(deployment_id)["source"] == "agent":
            self.store.reset_rollback_count()
        self._state(deployment_id, "Settled", finished=True)
        try:
            self.store.set_commit(release_id, self.history.record(release_id, self.releases.path(release_id), message))
            self.history.push()
        except Exception as exc:
            alerts.send(self.cfg, "warning", "History wasn't updated", str(exc)[:500])
        self._report(deployment_id, "Settled", None, message, "release settled")
        self.store.audit("guard", "settled", {"deployment": deployment_id, "release": release_id})
        self.releases.prune(self.cfg.safety.keep_releases)
        return Outcome("Settled", deployment_id, release_id)

    def _rollback(self, deployment_id, previous, failed_release, check, reason) -> Outcome:
        self._state(deployment_id, "RollingBack", failed_check=check)
        if failed_release:
            self.store.set_release_state(failed_release, "failed")
        trouble = None
        try:
            if not previous:
                raise RuntimeError("there's no previous release to go back to")
            if not self.releases.path(previous).is_dir() and not backup.restore_code(self.cfg, self.store, previous):
                raise RuntimeError(f"previous release {previous} is missing and has no backup")
            self.hook("RollbackReady")
            self.releases.switch(previous)
            self.hook("RollbackSwitched")
            self.pm.restart()
            trouble = health.startup_failure(self.cfg, self.pm)
        except Exception as exc:
            self.log.exception("rollback failed")
            trouble = str(exc)
        source = self.store.deployment(deployment_id)["source"]
        if trouble:
            return self._needs_human(deployment_id, check, reason, trouble)
        self._state(deployment_id, "RolledBack", finished=True)
        self._report(deployment_id, "RolledBack", check, reason, f"brought back {previous}")
        self.store.audit("guard", "rolled_back", {"deployment": deployment_id, "to": previous, "why": reason})
        if source != "practice":
            alerts.send(self.cfg, "warning", f"Rolled back to {previous}", reason[:500])
        if source == "agent" and self.store.count_rollback() >= self.cfg.limits.breaker_after:
            self.store.open_breaker(f"{self.cfg.limits.breaker_after} rollbacks in a row")
            alerts.send(self.cfg, "urgent", "Circuit breaker open", "Run livingcode resume after looking at it.")
        return Outcome("RolledBack", deployment_id, previous, check, reason)

    def _needs_human(self, deployment_id, check, reason, trouble) -> Outcome:
        self._state(deployment_id, "NeedsHuman", finished=True)
        self.store.open_breaker(f"rollback failed: {trouble[:200]}")
        self._report(deployment_id, "NeedsHuman", check, f"{reason}; rollback failed: {trouble}",
                     "stopped everything, a person must look")
        alerts.send(self.cfg, "urgent", "Rollback failed, a person must look", trouble[:500])
        self.store.audit("guard", "needs_human", {"deployment": deployment_id, "trouble": trouble})
        return Outcome("NeedsHuman", deployment_id, None, check, trouble)

    def manual_rollback(self, target: str | None = None) -> Outcome:
        with self._exclusive():
            current = self.releases.current()
            settled = [r["id"] for r in self.store.releases() if r["state"] == "settled" and r["id"] != current]
            if target is None:
                earlier = [item for item in settled if item < current]
                target = earlier[0] if earlier else None
            if not target or target not in settled:
                raise RuntimeError("there's no such earlier release to go back to")
            deployment_id = self.store.new_deployment("manual-rollback", target, "human")
            return self._rollback(deployment_id, target, None, "manual", "operator asked for a rollback")

    def recover(self) -> Outcome | None:
        """Runs on every guard start. Finishes whatever a crash or restart cut off."""
        with self._exclusive():
            row = self.store.open_deployment()
            self.releases.clean_stages()
            if row is None:
                if self.releases.current() and not self.pm.is_active():
                    self.pm.start()
                return None
            deployment_id, state = row["id"], row["state"]
            if state in ("Checking", "BackingUp", "Building"):
                outcome = self._stop(deployment_id, "interrupted", f"guard restarted during {state}")
                if not self.pm.is_active():
                    self.pm.start()
                return outcome
            return self._rollback(deployment_id, row["previous_release"], row["release_id"],
                                  "interrupted", f"guard restarted during {state}")

    def resume(self) -> None:
        self.store.close_breaker()
        self.store.audit("operator", "resumed")

    def practice_rollback(self) -> str | None:
        """Used by the doctor. Really switches away and back, on a copy of the live release."""
        with self._exclusive():
            current = self.releases.current()
            if not current:
                return "there's no live release yet"
            deployment_id = self.store.new_deployment("practice", current, "practice")
            stage = self.releases.stage_from(self.releases.path(current), str(deployment_id))
            release_id = self.store.next_release_id()
            self.releases.finalize(stage, release_id)
            self.store.add_release(release_id, "practice", "practice")
            self._state(deployment_id, "Switching", release_id=release_id)
            self.releases.switch(release_id)
            self.pm.restart()
            outcome = self._rollback(deployment_id, current, None, "practice", "practice rollback")
            self.releases.remove(release_id)
            os.unlink(self.cfg.reports_dir / f"{deployment_id:05d}.json")
            if outcome.state != "RolledBack":
                return outcome.detail
            self.store.audit("guard", "practice_rollback", "passed")
            return None
