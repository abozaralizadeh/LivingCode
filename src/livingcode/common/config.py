"""Reads and checks the config file. Standard library only, so the guard can use it."""
from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CAPABILITIES = (
    "bug_fixing",
    "updating",
    "fixing_vulnerabilities",
    "watching_user_behaviour",
    "adding_features",
)
MODES = ("off", "propose", "auto")
ENABLED_LINE = re.compile(r"^enabled\s*=.*$", re.MULTILINE)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class App:
    name: str = ""
    service: str = ""
    repo: str = ""
    root: str = ""
    runner: str = "local"
    start_command: tuple = ("{python}", "{current}/app.py")
    test_command: tuple = ("{python}", "-m", "unittest", "discover", "-s", "tests")
    test_timeout_seconds: float = 120.0
    log_files: tuple = ()
    env: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Paths:
    state_dir: str = ""
    log_dir: str = ""


@dataclass(frozen=True)
class LLM:
    provider: str = "fake"
    model: str = "set-your-model-here"
    base_url: str = ""
    key_file: str = ""
    fake_script: str = ""
    # Only for the relay provider: the unix socket of the web demo's front process.
    socket: str = ""
    budget_per_round_usd: float = 2.0
    budget_per_month_usd: float = 20.0
    input_usd_per_mtok: float = 4.0
    output_usd_per_mtok: float = 20.0
    max_tokens: int = 8000
    max_tries: int = 5
    max_turns: int = 20


@dataclass(frozen=True)
class Capabilities:
    bug_fixing: str = "propose"
    updating: str = "off"
    fixing_vulnerabilities: str = "off"
    watching_user_behaviour: str = "off"
    adding_features: str = "off"


@dataclass(frozen=True)
class Limits:
    rounds_per_day: int = 3
    deploys_per_day: int = 1
    rollback_wait_hours: float = 24.0
    max_changed_files: int = 20
    max_changed_lines: int = 500
    feature_max_changed_files: int = 10
    feature_max_changed_lines: int = 300
    max_attempts: int = 2
    breaker_after: int = 3
    track_record_releases: int = 10
    update_wait_days: int = 7


@dataclass(frozen=True)
class Health:
    url: str = ""
    startup_seconds: float = 60.0
    ok_count: int = 5
    window_seconds: float = 60.0
    interval_seconds: float = 1.0
    probation_minutes: float = 15.0
    poll_seconds: float = 15.0
    error_rate_factor: float = 3.0
    min_errors: int = 5
    smoke_checks: tuple = ()


@dataclass(frozen=True)
class Safety:
    protected_paths: tuple = ("migrations/", "deploy/", "auth/", "payments/")
    health_paths: tuple = ("health.py",)
    tests_path: str = "tests/"
    backup_command: str = ""
    alert_webhook: str = ""
    keep_releases: int = 5
    new_dependency_capabilities: tuple = ()
    scanners: tuple = ("builtin", "advisories")


@dataclass(frozen=True)
class Sources:
    package_index: str = ""
    registry_file: str = ""
    advisories_file: str = ""
    features_file: str = ""


@dataclass(frozen=True)
class Sync:
    backend: str = "local"
    branch: str = "livingcode/live"
    push: bool = False


@dataclass(frozen=True)
class Behaviour:
    retention_days: int = 7
    min_count: int = 5


@dataclass(frozen=True)
class Config:
    path: Path
    enabled: bool
    app: App
    paths: Paths
    llm: LLM
    capabilities: Capabilities
    limits: Limits
    health: Health
    safety: Safety
    sources: Sources
    sync: Sync
    behaviour: Behaviour

    def mode(self, capability: str) -> str:
        return getattr(self.capabilities, capability)

    @property
    def app_root(self) -> Path:
        return Path(self.app.root).resolve()

    @property
    def repo(self) -> Path:
        return Path(self.app.repo).resolve()

    @property
    def releases_dir(self) -> Path:
        return self.app_root / "releases"

    @property
    def current_link(self) -> Path:
        return self.app_root / "current"

    @property
    def shared_dir(self) -> Path:
        return self.app_root / "shared"

    @property
    def state_dir(self) -> Path:
        return Path(self.paths.state_dir).resolve()

    @property
    def log_dir(self) -> Path:
        return Path(self.paths.log_dir).resolve()

    @property
    def guard_dir(self) -> Path:
        return self.state_dir / "guard"

    @property
    def agent_dir(self) -> Path:
        return self.state_dir / "agent"

    @property
    def candidates_dir(self) -> Path:
        return self.state_dir / "candidates"

    @property
    def proposals_dir(self) -> Path:
        return self.state_dir / "proposals"

    @property
    def approvals_dir(self) -> Path:
        return self.guard_dir / "approvals"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"

    @property
    def events_dir(self) -> Path:
        return self.state_dir / "events"

    @property
    def backups_dir(self) -> Path:
        return self.guard_dir / "backups"

    def all_dirs(self) -> list[Path]:
        return [
            self.releases_dir, self.shared_dir, self.guard_dir, self.agent_dir,
            self.candidates_dir, self.proposals_dir, self.approvals_dir,
            self.reports_dir, self.events_dir, self.backups_dir, self.log_dir,
        ]


def _section(cls, raw, where):
    raw = dict(raw or {})
    base = cls()
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"unknown setting in [{where}]: {', '.join(unknown)}")
    values = {}
    for key, value in raw.items():
        want = type(getattr(base, key))
        if want is tuple and isinstance(value, list):
            value = tuple(value)
        elif want is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise ConfigError(f"[{where}] {key} should be a {want.__name__}")
        values[key] = value
    return cls(**values)


def _validate(cfg: Config) -> None:
    for name, value in (
        ("app.name", cfg.app.name), ("app.repo", cfg.app.repo), ("app.root", cfg.app.root),
        ("paths.state_dir", cfg.paths.state_dir), ("paths.log_dir", cfg.paths.log_dir),
        ("health.url", cfg.health.url),
    ):
        if not value:
            raise ConfigError(f"{name} is required")
    if cfg.app.runner not in ("local", "systemd"):
        raise ConfigError("app.runner must be local or systemd")
    if cfg.app.runner == "systemd" and not cfg.app.service:
        raise ConfigError("app.service is required with the systemd runner")
    for capability in CAPABILITIES:
        if cfg.mode(capability) not in MODES:
            raise ConfigError(f"capabilities.{capability} must be one of {', '.join(MODES)}")
    if cfg.llm.provider not in ("fake", "anthropic", "openai", "relay"):
        raise ConfigError("llm.provider must be fake, anthropic, openai or relay")
    if cfg.llm.provider == "relay" and not cfg.llm.socket:
        raise ConfigError("llm.socket is required with the relay provider")
    if cfg.sync.backend not in ("local", "git"):
        raise ConfigError("sync.backend must be local or git")
    for check in cfg.health.smoke_checks:
        if not isinstance(check, dict) or "url" not in check:
            raise ConfigError("each health.smoke_checks entry needs a url")
    for capability in cfg.safety.new_dependency_capabilities:
        if capability not in CAPABILITIES:
            raise ConfigError(f"unknown capability in new_dependency_capabilities: {capability}")


def parse(text: str, path: Path) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config isn't valid TOML: {exc}") from exc
    sections = {
        "app": App, "paths": Paths, "llm": LLM, "capabilities": Capabilities, "limits": Limits,
        "health": Health, "safety": Safety, "sources": Sources, "sync": Sync, "behaviour": Behaviour,
    }
    unknown = sorted(set(raw) - set(sections) - {"enabled"})
    if unknown:
        raise ConfigError(f"unknown setting: {', '.join(unknown)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("enabled must be true or false")
    built = {name: _section(cls, raw.get(name), name) for name, cls in sections.items()}
    cfg = Config(path=path, enabled=enabled, **built)
    _validate(cfg)
    return cfg


def load(path: str | os.PathLike) -> Config:
    path = Path(path).resolve()
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"can't read config {path}: {exc}") from exc
    return parse(text, path)


def set_enabled(path: Path, value: bool) -> None:
    text = path.read_text()
    line = f"enabled = {'true' if value else 'false'}"
    updated, count = ENABLED_LINE.subn(line, text, count=1)
    if not count:
        updated = line + "\n" + text
    tmp = path.with_suffix(".tmp")
    tmp.write_text(updated)
    os.replace(tmp, path)


def fingerprint(path: Path) -> str:
    """Hash of the config, ignoring the flag, so turning it on keeps a doctor pass valid."""
    text = ENABLED_LINE.sub("", path.read_text())
    return hashlib.sha256(text.encode()).hexdigest()
