"""The livingcode command. Everything an operator does goes through here."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from .common import config as config_module, lab
from .common.lab import LabLockError
from .common.logs import append_limited


def config_path(args) -> Path:
    value = args.config or os.environ.get("LIVINGCODE_CONFIG") or (lab.PROJECT_ROOT / "lab/etc/sample.toml")
    return Path(value)


def load(args):
    return config_module.load(config_path(args))


def deployer(args):
    from .guard.deploy import Deployer

    return Deployer(load(args))


def agent(args):
    from .agent.service import Agent

    return Agent(load(args))


def ago(stamp) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else "-"


def cmd_init(args) -> int:
    from . import labtools

    base = Path(args.dir or labtools.DEFAULT_LAB)
    path = labtools.setup(base, args.port)
    print(f"Wrote {path}. The feature flag is off and bug fixing is on propose.")
    return 0


def cmd_install(args) -> int:
    release = deployer(args).bootstrap()
    print(f"Installed. Live release: {release}")
    return 0


def cmd_doctor(args) -> int:
    cfg = load(args)
    failed = False

    def line(ok, text):
        nonlocal failed
        failed = failed or not ok
        print(f"{'OK  ' if ok else 'FAIL'} {text}")

    line(True, f"config is valid: {cfg.path}")
    found = lab.problems(cfg) if lab.LAB_ONLY else []
    line(not found, "target is the throwaway sample" if not found else "; ".join(found))
    if found:
        return 1
    from .common.sandbox import SandboxUnavailable, make_sandbox
    from .guard import health
    from .guard.deploy import Deployer

    try:
        make_sandbox(cfg)
        line(True, "sandbox is available")
    except SandboxUnavailable as exc:
        line(False, str(exc))
    if cfg.llm.provider != "fake":
        from .llm import LLMError, make_client

        try:
            make_client(cfg)
            line(True, "LLM key found")
        except (LLMError, OSError) as exc:
            line(False, str(exc))
    else:
        print("NOTE using the local fake LLM, so no key is needed and nothing is paid for")
    if platform.system() == "Linux" and os.geteuid() == 0:
        line(cfg.guard_dir.stat().st_uid != cfg.agent_dir.stat().st_uid, "guard and agent have separate owners")
    else:
        print("WARN separate guard and agent users aren't in place on this machine, see README")
    guard = Deployer(cfg)
    active = guard.pm.is_active()
    line(active, "the app is running")
    if active:
        status, _ = health.fetch(cfg.health.url)
        line(status == 200, f"health URL answers: {status}")
        failure = health.smoke_failure(cfg)
        line(failure is None, failure or "smoke checks pass")
        trouble = guard.practice_rollback()
        line(trouble is None, trouble or "practice rollback worked")
    if not failed:
        guard.store.set_meta("doctor_passed", config_module.fingerprint(cfg.path))
    print("Doctor passed." if not failed else "Doctor found problems.")
    return 1 if failed else 0


def cmd_on(args) -> int:
    from .guard.store import GuardStore

    cfg = load(args)
    lab.check(cfg)
    store = GuardStore(cfg.guard_dir / "guard.db")
    if store.get_meta("doctor_passed") != config_module.fingerprint(cfg.path):
        print("Run livingcode doctor first. It has to pass on this exact config.")
        return 1
    config_module.set_enabled(cfg.path, True)
    store.audit("operator", "flag_on")
    print("The feature flag is on.")
    return 0


def cmd_off(args) -> int:
    path = config_path(args)
    config_module.set_enabled(path, False)
    print("The feature flag is off. No new rounds start and candidates are refused.")
    return 0


def cmd_status(args) -> int:
    guard = deployer(args)
    cfg = guard.cfg
    from .agent.gate import month_start
    from .agent.store import AgentStore

    store = AgentStore(cfg.agent_dir / "agent.db")
    breaker = guard.store.breaker()
    waiting = [p for p in cfg.proposals_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    print(f"Feature flag:   {'on' if cfg.enabled else 'off'}")
    print(f"Live release:   {guard.releases.current() or 'none'}")
    print(f"App running:    {'yes' if guard.pm.is_active() else 'no'}")
    print(f"Breaker:        {breaker['state']}" + (f" ({breaker['reason']})" if breaker["reason"] else ""))
    print(f"Rollbacks in a row: {breaker['consecutive_rollbacks']}")
    print(f"Waiting proposals:  {len(waiting)}")
    print(f"LLM spent this month: ${store.spent_since(month_start()):.4f} of ${cfg.llm.budget_per_month_usd:.2f}")
    return 0


def cmd_run(args) -> int:
    result = agent(args).run_round("manual")
    print(f"{result.status}: {result.detail}")
    if result.candidate_id:
        print(f"Candidate: {result.candidate_id}")
    return 0


def cmd_proposals(args) -> int:
    items = agent(args).proposals()
    for item in items:
        print(f"{item['id']}  {item['capability']}  {item['summary']}")
    if not items:
        print("No proposals are waiting.")
    return 0


def find_candidate(cfg, candidate_id: str) -> Path | None:
    if not candidate_id or "/" in candidate_id or candidate_id.startswith("."):
        return None
    for folder in (cfg.proposals_dir, cfg.candidates_dir, cfg.guard_dir / "processed"):
        if (folder / candidate_id).is_dir():
            return folder / candidate_id
    return None


def cmd_show(args) -> int:
    from .guard import candidates

    cfg = load(args)
    folder = find_candidate(cfg, args.id)
    if folder is None:
        print("No such candidate.")
        return 1
    candidate = candidates.load(folder)
    print(f"Candidate:  {candidate.id}\nCapability: {candidate.capability}\nBuilt on:   {candidate.base_release}")
    print(f"Why:        {candidate.summary}\n")
    base = cfg.releases_dir / candidate.base_release
    for change in candidate.changes:
        target = base / change.path
        before = target.read_text(errors="replace").splitlines() if target.is_file() else []
        after = change.content.decode(errors="replace").splitlines() if change.content is not None else []
        for text in difflib.unified_diff(before, after, f"a/{change.path}", f"b/{change.path}", lineterm=""):
            print(text)
    return 0


def cmd_approve(args) -> int:
    guard = deployer(args)
    cfg = guard.cfg
    folder = cfg.proposals_dir / args.id
    if not args.id or "/" in args.id or not folder.is_dir():
        print("No such proposal.")
        return 1
    digest = hashlib.sha256((folder / "changes.json").read_bytes()).hexdigest()
    (cfg.approvals_dir / f"{args.id}.json").write_text(json.dumps({"changes_sha256": digest, "time": time.time()}))
    os.replace(folder, cfg.candidates_dir / args.id)
    guard.store.audit("operator", "approved", args.id)
    outcome = guard.process_candidate(cfg.candidates_dir / args.id)
    print(f"{outcome.state}: {outcome.detail or outcome.release_id}")
    return 0 if outcome.state == "Settled" else 1


def cmd_reject(args) -> int:
    from .agent.store import AgentStore

    guard = deployer(args)
    cfg = guard.cfg
    folder = cfg.proposals_dir / args.id
    if not args.id or "/" in args.id or not folder.is_dir():
        print("No such proposal.")
        return 1
    issue_id = json.loads((folder / "manifest.json").read_text()).get("issue_id", "")
    if str(issue_id).isdigit():
        AgentStore(cfg.agent_dir / "agent.db").set_issue(int(issue_id), status="rejected")
    shutil.rmtree(folder)
    guard.store.audit("operator", "rejected_proposal", args.id)
    print("Rejected.")
    return 0


def cmd_deploy(args) -> int:
    outcome = deployer(args).deploy_ref(args.ref)
    print(f"{outcome.state}: {outcome.detail or outcome.release_id}")
    return 0 if outcome.state == "Settled" else 1


def cmd_history(args) -> int:
    guard = deployer(args)
    current = guard.releases.current()
    for row in guard.store.releases():
        mark = "*" if row["id"] == current else " "
        print(f"{mark} {row['id']}  {row['state']:<9} {row['source']:<8} {ago(row['created_at'])}")
    print()
    for row in guard.store.deployments():
        check = f"  failed: {row['failed_check']}" if row["failed_check"] else ""
        print(f"  #{row['id']}  {row['state']:<11} {row['source']:<8} {row['capability'] or '-'}{check}")
    return 0


def cmd_rollback(args) -> int:
    outcome = deployer(args).manual_rollback(args.release)
    print(f"{outcome.state}: now on {outcome.release_id}" if outcome.release_id else f"{outcome.state}: {outcome.detail}")
    return 0 if outcome.state == "RolledBack" else 1


def cmd_resume(args) -> int:
    deployer(args).resume()
    print("The circuit breaker is closed.")
    return 0


def cmd_cost(args) -> int:
    from .agent.gate import month_start
    from .agent.store import AgentStore

    cfg = load(args)
    store = AgentStore(cfg.agent_dir / "agent.db")
    print(f"Today:      ${store.spent_since(time.time() - 86400):.4f}")
    print(f"This month: ${store.spent_since(month_start()):.4f} of ${cfg.llm.budget_per_month_usd:.2f}")
    return 0


def cmd_guard_service(args) -> int:
    from .guard import service

    if args.once:
        guard = deployer(args)
        guard.recover()
        print(f"Handled {service.tick(guard)} candidates.")
        return 0
    service.run_forever(config_path(args))
    return 0


def cmd_agent_service(args) -> int:
    from .agent import service

    if args.once:
        result = agent(args).tick()
        print(result or "Nothing to do.")
        return 0
    service.run_forever(config_path(args))
    return 0


def cmd_lab(args) -> int:
    from . import labtools

    if args.action == "stop":
        deployer(args).pm.stop()
        print("The sample app is stopped.")
    elif args.action == "restart":
        deployer(args).pm.restart()
        print("The sample app is restarted.")
    elif args.action == "reset":
        path = config_path(args)
        if path.exists():
            deployer(args).pm.stop()
        shutil.rmtree(labtools.DEFAULT_LAB, ignore_errors=True)
        print("The lab is gone.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="livingcode")
    parser.add_argument("--config", help="path of the config file")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    init = add("init", cmd_init, "write a config for the throwaway sample")
    init.add_argument("--dir")
    init.add_argument("--port", type=int, default=18080)
    add("install", cmd_install, "create the first release and start the app")
    add("doctor", cmd_doctor, "check the setup, including a practice rollback")
    add("on", cmd_on, "turn the feature flag on")
    add("off", cmd_off, "turn the feature flag off")
    add("status", cmd_status, "show the flag, live release, breaker and budget")
    add("run", cmd_run, "start a round now")
    add("proposals", cmd_proposals, "list changes waiting for approval")
    add("show", cmd_show, "show a change, its reason and its diff").add_argument("id")
    add("approve", cmd_approve, "approve a proposal and deploy it").add_argument("id")
    add("reject", cmd_reject, "reject a proposal").add_argument("id")
    add("deploy", cmd_deploy, "deploy a person's change through the same safe path").add_argument("ref")
    add("history", cmd_history, "list releases and deployments")
    add("rollback", cmd_rollback, "go back to an earlier release").add_argument("release", nargs="?")
    add("resume", cmd_resume, "close the circuit breaker")
    add("cost", cmd_cost, "show LLM spending")
    add("guard-service", cmd_guard_service, "run the guard").add_argument("--once", action="store_true")
    add("agent-service", cmd_agent_service, "run the agent").add_argument("--once", action="store_true")
    add("lab", cmd_lab, "manage the lab").add_argument("action", choices=("stop", "restart", "reset"))
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (LabLockError, config_module.ConfigError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        try:
            cfg = config_module.load(config_path(args))
            if not lab.problems(cfg):
                append_limited(cfg.log_dir / "cli-errors.log", f"{time.time()} {exc}")
        except (config_module.ConfigError, OSError):
            pass
        return 1
