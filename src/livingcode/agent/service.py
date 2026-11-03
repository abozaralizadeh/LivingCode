"""One improvement round from start to finish, plus the agent's main loop."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..common import alerts, config as config_module, lab, lockfile
from ..common.advisories import make_advisories
from ..common.installer import InstallError, make_installer, read_pins
from ..common.logs import get_logger
from ..common.pathrules import LOCK_FILE
from ..common.sandbox import SandboxUnavailable, make_sandbox
from ..llm import LLMError, make_client
from . import behaviour, errors
from .gate import BudgetExceeded, SafeLLM, wrap_untrusted
from .loop import Improver, LoopResult
from .registry import make_registry
from .store import AgentStore
from .workspace import Workspace

CAPABILITY_ORDER = ("fixing_vulnerabilities", "bug_fixing", "updating", "watching_user_behaviour", "adding_features")


@dataclass
class RoundResult:
    status: str
    detail: str = ""
    issue_id: int | None = None
    candidate_id: str | None = None


class Agent:
    def __init__(self, cfg, client=None, sandbox=None, installer=None, advisories=None, registry=None):
        lab.check(cfg)
        self.cfg = cfg
        for folder in (cfg.agent_dir, cfg.candidates_dir, cfg.proposals_dir, cfg.events_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.store = AgentStore(cfg.agent_dir / "agent.db")
        self.client = client or make_client(cfg)
        self.sandbox = sandbox or make_sandbox(cfg)
        self.installer = installer or make_installer(cfg)
        self.advisories = advisories or make_advisories(cfg)
        self.registry = registry or make_registry(cfg)
        self.log = get_logger(cfg.log_dir, "agent")

    def current_release(self) -> str | None:
        link = self.cfg.current_link
        return os.readlink(link).rsplit("/", 1)[-1] if link.is_symlink() else None

    def _block_reason(self) -> str | None:
        now = time.time()
        if not self.cfg.enabled:
            return "the feature flag is off"
        if not self.current_release():
            return "there's no live release yet"
        if self.store.rounds_since(now - 86400) >= self.cfg.limits.rounds_per_day:
            return "the daily round limit is used up"
        last = self.store.get_meta("last_rollback", 0)
        if now - last < self.cfg.limits.rollback_wait_hours * 3600:
            return "still in the wait after a rollback"
        return None

    def read_reports(self) -> int:
        """Reads the guard's reports. The guard never waits for this."""
        seen = self.store.get_meta("reports_seen", 0)
        count = 0
        for path in sorted(self.cfg.reports_dir.glob("[0-9]*.json")):
            number = int(path.stem)
            if number <= seen:
                continue
            report = json.loads(path.read_text())
            self._apply_report(report, str(path))
            seen = number
            count += 1
        self.store.set_meta("reports_seen", seen)
        return count

    def _apply_report(self, report: dict, path: str) -> None:
        if report.get("outcome") in ("RolledBack", "NeedsHuman") and report.get("source") == "agent":
            self.store.set_meta("last_rollback", report.get("time", time.time()))
        issue_id = report.get("issue_id")
        issue = self.store.issue(int(issue_id)) if str(issue_id or "").isdigit() else None
        if issue is None:
            return
        if report["outcome"] == "Settled":
            self.store.set_issue(issue["id"], status="fixed")
        elif not report.get("counts_as_attempt", True):
            self.store.set_issue(issue["id"], status="open")
        else:
            self._failed_attempt(issue["id"], path)

    def _failed_attempt(self, issue_id: int, report_path: str | None = None) -> None:
        issue = self.store.issue(issue_id)
        attempts = issue["attempts"] + 1
        if attempts >= self.cfg.limits.max_attempts:
            self.store.set_issue(issue_id, status="needs_person", attempts=attempts, last_report=report_path)
            alerts.send(self.cfg, "warning", "An issue needs a person", issue["title"][:300])
        else:
            self.store.set_issue(issue_id, status="open", attempts=attempts, last_report=report_path)

    def collect_errors(self) -> int:
        offsets = self.store.get_meta("log_offsets", {})
        text, offsets = errors.read_new(self.cfg.app.log_files, offsets)
        blocks = errors.parse_blocks(text)
        extra, offsets = errors.read_library_events(self.cfg.events_dir, offsets)
        self.store.set_meta("log_offsets", offsets)
        new = 0
        for block in blocks + extra:
            issue_id, created = self.store.add_issue(f"bug:{block.fingerprint}", "bug_fixing", block.title, block.text)
            if created:
                self.store.add_trigger("new_error", issue_id, "issue opened")
                new += 1
        return new

    def _collect_vulnerabilities(self, pins: dict) -> None:
        for item in self.advisories.check(pins):
            self.store.add_issue(
                f"vuln:{item.id}:{item.package}", "fixing_vulnerabilities",
                f"{item.id} in {item.package} {item.version}",
                json.dumps({"package": item.package, "fixed": item.fixed_version, "id": item.id}),
            )

    def _collect_updates(self, pins: dict) -> None:
        for pin in pins.values():
            found = self.registry.newest_after_wait(pin.name, pin.version, self.cfg.limits.update_wait_days)
            if found:
                self.store.add_issue(
                    f"update:{pin.name}:{found.version}", "updating", f"Update {pin.name} to {found.version}",
                    json.dumps({"package": pin.name, "fixed": found.version}),
                )

    def _collect_behaviour(self) -> None:
        behaviour.purge_old(self.cfg.events_dir, self.cfg.behaviour.retention_days)
        totals = behaviour.totals(behaviour.read_events(self.cfg.events_dir), self.cfg.behaviour.min_count)
        if totals["routes"]:
            week = time.strftime("%G-%V", time.gmtime())
            self.store.add_issue(f"behaviour:{week}", "watching_user_behaviour",
                                 f"Behaviour review for week {week}", json.dumps(totals))

    def _collect_features(self) -> None:
        path = self.cfg.sources.features_file
        if path and Path(path).is_file():
            with open(path, "rb") as handle:
                features = tomllib.load(handle).get("feature", [])
            for item in features:
                self.store.add_issue(f"feature:{item['id']}", "adding_features", item.get("title", item["id"]),
                                     json.dumps({"id": item["id"], "origin": "operator",
                                                 "description": item.get("description", "")}))
        if not self.store.open_issues():
            day = time.strftime("%Y-%m-%d", time.gmtime())
            self.store.add_issue(f"feature-suggestion:{day}", "adding_features", "Suggest one small feature",
                                 json.dumps({"id": "", "origin": "agent", "description": ""}))

    def collect_issues(self) -> None:
        cfg = self.cfg
        self.collect_errors()
        pins = read_pins(cfg.current_link)
        if cfg.mode("fixing_vulnerabilities") != "off":
            self._collect_vulnerabilities(pins)
        if cfg.mode("updating") != "off":
            self._collect_updates(pins)
        if cfg.mode("watching_user_behaviour") != "off":
            self._collect_behaviour()
        if cfg.mode("adding_features") != "off":
            self._collect_features()

    def pick_issue(self, wanted: int | None = None):
        issues = [i for i in self.store.open_issues() if self.cfg.mode(i["capability"]) != "off"]
        if wanted is not None:
            issues = [i for i in issues if i["id"] == wanted]
        issues.sort(key=lambda i: (i["last_report"] is None, CAPABILITY_ORDER.index(i["capability"]), i["id"]))
        return issues[0] if issues else None

    def _task(self, issue) -> str:
        capability = issue["capability"]
        detail = issue["detail"]
        report = ""
        if issue["last_report"] and Path(issue["last_report"]).is_file():
            data = json.loads(Path(issue["last_report"]).read_text())
            text = f"failed check: {data.get('failed_check')}\n{data.get('detail')}\n{data.get('log_excerpt')}"
            report = "\n\nYour last attempt failed. The guard's report:\n" + wrap_untrusted("guard-report", text)
        if capability == "bug_fixing":
            body = "Fix the bug behind this error:\n" + wrap_untrusted("application-log", detail)
        elif capability == "watching_user_behaviour":
            body = ("Here are visit totals per page. Suggest and make one small code change that helps users:\n"
                    + wrap_untrusted("behaviour-totals", detail))
        elif capability == "adding_features":
            info = json.loads(detail)
            if info["origin"] == "operator":
                body = "Build this feature:\n" + wrap_untrusted("feature-request", f"{issue['title']}\n{info['description']}")
            else:
                body = "Suggest one small, useful feature for this project and build it."
        else:
            info = json.loads(detail)
            body = (f"{info['package']} was moved to {info['fixed']} in {LOCK_FILE}, and now the tests fail. "
                    "Fix the code so it works with the new version.")
        return body + report

    def _bump_pin(self, workspace: Workspace, info: dict) -> None:
        version = self.registry.find(info["package"], info["fixed"])
        if version is None:
            raise InstallError(f"{info['package']} {info['fixed']} isn't in the registry")
        lock = workspace.root / LOCK_FILE
        pins = lockfile.parse(lock.read_text())
        pins[info["package"]] = lockfile.Pin(info["package"], version.version, version.sha256)
        lock.write_text(lockfile.render(pins))
        self.installer.install(workspace.root)

    def _improve(self, issue, workspace: Workspace, llm: SafeLLM) -> LoopResult:
        improver = Improver(self.cfg, llm, workspace)
        if issue["capability"] in ("updating", "fixing_vulnerabilities"):
            self._bump_pin(workspace, json.loads(issue["detail"]))
            passed, _ = workspace.run_tests()
            if passed:
                return LoopResult(True, issue["title"])
        return improver.run(self._task(issue))

    def _submit(self, issue, base: str, changes: list[dict], summary: str, round_id: int) -> tuple[str, str]:
        cfg = self.cfg
        raw = json.dumps(changes, sort_keys=True).encode()
        candidate_id = f"c{round_id:05d}-{hashlib.sha256(raw).hexdigest()[:10]}"
        capability = issue["capability"]
        feature_id = ""
        if capability == "adding_features":
            feature_id = json.loads(issue["detail"]).get("id", "")
        mode = cfg.mode(capability)
        waits = (
            mode == "propose" or capability == "watching_user_behaviour"
            or (capability == "adding_features" and not feature_id)
        )
        manifest = {
            "id": candidate_id, "base_release": base, "capability": capability,
            "issue_id": str(issue["id"]), "summary": summary, "feature_id": feature_id,
            "created_at": time.time(),
        }
        folder = cfg.proposals_dir if waits else cfg.candidates_dir
        tmp = folder / f".tmp-{candidate_id}"
        tmp.mkdir()
        (tmp / "changes.json").write_bytes(raw)
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        os.replace(tmp, folder / candidate_id)
        return candidate_id, "proposed" if waits else "submitted"

    def run_round(self, trigger: str = "manual", wanted: int | None = None) -> RoundResult:
        with open(self.cfg.agent_dir / "round.lock", "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return RoundResult("skipped", "a round is already running")
            return self._run_round(trigger, wanted)

    def _run_round(self, trigger: str, wanted: int | None) -> RoundResult:
        self.read_reports()
        reason = self._block_reason()
        if reason:
            self.store.add_trigger(trigger, outcome=f"skipped: {reason}")
            return RoundResult("skipped", reason)
        self.collect_issues()
        issue = self.pick_issue(wanted)
        trigger_id = self.store.add_trigger(trigger, issue["id"] if issue else None)
        if issue is None:
            self.store.set_trigger(trigger_id, "nothing to do")
            return RoundResult("idle", "nothing to do")
        round_id = self.store.start_round(trigger_id, issue["id"])
        self.store.set_issue(issue["id"], status="in_progress")
        llm = SafeLLM(self.cfg, self.client, self.store, round_id)
        base = self.current_release()
        workspace = Workspace(self.cfg, self.cfg.releases_dir / base,
                              self.cfg.agent_dir / "workspace" / f"round-{round_id}", self.sandbox)
        try:
            result = self._improve(issue, workspace, llm)
            changes = workspace.changes() if result.ok else []
            if result.ok and not changes:
                result = LoopResult(False, "nothing changed")
            if not result.ok:
                return self._no_fix(issue, round_id, trigger_id, result.summary)
            candidate_id, status = self._submit(issue, base, changes, result.summary, round_id)
            self.store.set_issue(issue["id"], status="waiting")
            self.store.finish_round(round_id, status, result.summary)
            self.store.set_trigger(trigger_id, status)
            self.log.info("round %s %s: %s", round_id, status, result.summary)
            return RoundResult(status, result.summary, issue["id"], candidate_id)
        except (InstallError, LLMError, SandboxUnavailable, BudgetExceeded, lockfile.LockError, OSError) as exc:
            self.log.exception("round failed")
            return self._no_fix(issue, round_id, trigger_id, f"{type(exc).__name__}: {exc}")
        finally:
            workspace.close()

    def _no_fix(self, issue, round_id, trigger_id, summary) -> RoundResult:
        self._failed_attempt(issue["id"])
        self.store.finish_round(round_id, "no_fix", summary)
        self.store.set_trigger(trigger_id, f"no fix: {summary[:200]}")
        self.log.info("round %s no fix: %s", round_id, summary)
        return RoundResult("no_fix", summary, issue["id"])

    def proposals(self) -> list[dict]:
        found = []
        for folder in sorted(self.cfg.proposals_dir.iterdir()):
            if folder.is_dir() and not folder.name.startswith("."):
                found.append(json.loads((folder / "manifest.json").read_text()))
        return found

    def tick(self) -> RoundResult | None:
        """One pass of the main loop: new errors and the daily schedule start a round."""
        self.read_reports()
        new_errors = self.collect_errors() if self.cfg.enabled else 0
        today = time.strftime("%Y-%m-%d", time.gmtime())
        scheduled = self.cfg.enabled and self.store.get_meta("last_scheduled") != today
        if not new_errors and not scheduled:
            return None
        if scheduled:
            self.store.set_meta("last_scheduled", today)
        return self.run_round("new_error" if new_errors else "schedule")


def run_forever(config_path, poll_seconds: float = 10.0) -> None:
    cfg = config_module.load(config_path)
    agent = Agent(cfg)
    while True:
        agent.cfg = config_module.load(config_path)
        try:
            agent.tick()
        except Exception:
            agent.log.exception("agent tick failed")
        shutil.rmtree(cfg.agent_dir / "workspace", ignore_errors=True)
        time.sleep(poll_seconds)
