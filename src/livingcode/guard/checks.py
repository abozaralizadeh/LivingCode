"""The fixed checks every candidate must pass before users can see it (see D7)."""
from __future__ import annotations

import difflib
import json
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..common.installer import InstallError, read_pins
from ..common.pathrules import is_clean, is_existing_test, is_protected, normalize
from ..common.sandbox import SandboxUnavailable
from .releases import apply_changes, sha256_of
from .scan import new_high_findings

VERSION_CHANGERS = ("updating", "fixing_vulnerabilities")


@dataclass
class Failure:
    check: str
    detail: str
    counts_as_attempt: bool = True


def _approved(cfg, candidate) -> bool:
    path = cfg.approvals_dir / f"{candidate.id}.json"
    try:
        approval = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return approval.get("changes_sha256") == candidate.changes_sha256


def _operator_feature(cfg, feature_id: str) -> bool:
    if not cfg.sources.features_file or not feature_id:
        return False
    try:
        with open(cfg.sources.features_file, "rb") as handle:
            features = tomllib.load(handle).get("feature", [])
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return any(item.get("id") == feature_id for item in features)


def check_gate(cfg, store, candidate) -> Failure | None:
    if not cfg.enabled:
        return Failure("flag", "the feature flag is off", False)
    if store.breaker()["state"] != "closed":
        return Failure("breaker", "the circuit breaker is open", False)
    if candidate.capability not in vars(cfg.capabilities):
        return Failure("capability", f"unknown capability: {candidate.capability}")
    mode = cfg.mode(candidate.capability)
    if mode == "off":
        return Failure("capability", f"{candidate.capability} is switched off")
    needs_approval = mode == "propose" or candidate.capability == "watching_user_behaviour"
    if candidate.capability == "adding_features" and not needs_approval:
        earned = store.clean_agent_releases() >= cfg.limits.track_record_releases
        needs_approval = not (earned and _operator_feature(cfg, candidate.feature_id))
    if needs_approval and not _approved(cfg, candidate):
        return Failure("approval", "this change needs the operator's approval", False)
    now = time.time()
    if store.agent_deploys_since(now - 86400) >= cfg.limits.deploys_per_day:
        return Failure("rate_limit", "the daily deploy limit is used up", False)
    last = store.last_agent_rollback()
    if last and now - last < cfg.limits.rollback_wait_hours * 3600:
        return Failure("rate_limit", "still in the wait after a rollback", False)
    return None


def check_paths(cfg, candidate, base: Path) -> Failure | None:
    if not candidate.changes:
        return Failure("paths", "the candidate changes nothing")
    folded = [normalize(change.path) for change in candidate.changes]
    if len(set(folded)) != len(folded):
        return Failure("paths", "the same path is changed twice")
    for change in candidate.changes:
        path = change.path
        if not is_clean(path):
            return Failure("paths", f"unsafe path: {path!r}")
        if is_protected(path, cfg.safety):
            return Failure("paths", f"protected path: {path}")
        target = base / path
        if target.is_symlink():
            return Failure("paths", f"path is a link: {path}")
        if is_existing_test(path, cfg.safety) and (change.action != "add" or target.exists()):
            return Failure("tests_changed", f"existing tests can't be edited or deleted: {path}")
        if change.action == "add" and target.exists():
            return Failure("base", f"file already exists: {path}")
        if change.action != "add" and sha256_of(target) != change.base_sha256:
            return Failure("base", f"file doesn't match the current release: {path}")
    return None


def _added_text(change, base: Path) -> tuple[list[str], int]:
    """Returns the added lines and the number of changed lines for one change."""
    before = []
    target = base / change.path
    if target.is_file():
        before = target.read_text(errors="replace").splitlines()
    after = change.content.decode(errors="replace").splitlines() if change.content is not None else []
    added, changed = [], 0
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        changed += 1
        if line.startswith("+"):
            added.append(line[1:])
    return added, changed


def check_size(cfg, candidate, base: Path) -> Failure | None:
    limits = cfg.limits
    features = candidate.capability == "adding_features"
    max_files = limits.feature_max_changed_files if features else limits.max_changed_files
    max_lines = limits.feature_max_changed_lines if features else limits.max_changed_lines
    if len(candidate.changes) > max_files:
        return Failure("size", f"{len(candidate.changes)} files changed, the limit is {max_files}")
    total = 0
    watched = _watched_routes(cfg)
    for change in candidate.changes:
        added, changed = _added_text(change, base)
        total += changed
        for line in added:
            if any(route in line for route in watched):
                return Failure("health_route", f"{change.path} touches a health or smoke route")
    if total > max_lines:
        return Failure("size", f"{total} lines changed, the limit is {max_lines}")
    return None


def _watched_routes(cfg) -> list[str]:
    urls = [cfg.health.url] + [check["url"] for check in cfg.health.smoke_checks]
    return sorted({urlparse(url).path for url in urls if len(urlparse(url).path) > 1})


def check_dependencies(cfg, candidate, base: Path, stage: Path) -> Failure | None:
    try:
        before = read_pins(base)
        after = read_pins(stage)
    except InstallError as exc:
        return Failure("dependencies", str(exc))
    new = sorted(set(after) - set(before))
    if new and candidate.capability not in cfg.safety.new_dependency_capabilities:
        return Failure("dependencies", f"new dependency isn't allowed: {', '.join(new)}")
    changed = [name for name in after if name in before and after[name] != before[name]]
    if changed and candidate.capability not in VERSION_CHANGERS:
        return Failure("dependencies", f"{candidate.capability} can't change versions: {', '.join(changed)}")
    return None


def check_scans(deployer, candidate, base: Path, stage: Path) -> Failure | None:
    paths = [change.path for change in candidate.changes]
    for scanner in deployer.scanners:
        try:
            findings = new_high_findings(scanner, base, stage, paths)
        except RuntimeError as exc:
            return Failure("scan", str(exc))
        if findings:
            first = findings[0]
            return Failure("scan", f"{first.rule} in {first.path}")
    if "advisories" in deployer.cfg.safety.scanners:
        try:
            before = {(a.id, a.package) for a in deployer.advisories.check(read_pins(base))}
            after = [a for a in deployer.advisories.check(read_pins(stage)) if (a.id, a.package) not in before]
        except (RuntimeError, ValueError, OSError) as exc:
            return Failure("scan", f"advisory check failed: {exc}")
        if after:
            return Failure("scan", f"{after[0].id} in {after[0].package} {after[0].version}")
    return None


def check_tests(deployer, stage: Path) -> Failure | None:
    cfg = deployer.cfg
    command = [part.replace("{python}", sys.executable) for part in cfg.app.test_command]
    try:
        result = deployer.sandbox.run(stage, command, cfg.app.test_timeout_seconds)
    except SandboxUnavailable as exc:
        return Failure("tests", f"no sandbox, so the tests can't run safely: {exc}", False)
    if result.code != 0:
        return Failure("tests", result.output[-1500:])
    return None


def run_all(deployer, candidate, base: Path, stage: Path) -> Failure | None:
    cfg = deployer.cfg
    failure = check_gate(cfg, deployer.store, candidate)
    if failure:
        return failure
    if candidate.base_release != deployer.releases.current():
        return Failure("base", "built on an old release", False)
    failure = check_paths(cfg, candidate, base) or check_size(cfg, candidate, base)
    if failure:
        return failure
    try:
        apply_changes(stage, candidate.changes)
    except (OSError, ValueError) as exc:
        return Failure("paths", str(exc))
    failure = check_dependencies(cfg, candidate, base, stage)
    if failure:
        return failure
    try:
        deployer.installer.install(stage)
    except InstallError as exc:
        return Failure("dependencies", str(exc))
    return check_scans(deployer, candidate, base, stage) or check_tests(deployer, stage)
