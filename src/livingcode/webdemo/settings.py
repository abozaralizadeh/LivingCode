"""Settings for the web demo, from the environment and an optional .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..common.lab import PROJECT_ROOT
from ..llm.azure import AzureTarget


class SettingsError(ValueError):
    pass


def read_env_file(path: Path) -> dict:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _int(env: dict, name: str, default: int, low: int = 0) -> int:
    raw = env.get(name, "")
    try:
        value = int(raw) if raw != "" else default
    except ValueError:
        raise SettingsError(f"{name} must be a whole number") from None
    if value < low:
        raise SettingsError(f"{name} must be {low} or more")
    return value


def _target(env: dict, prefix: str) -> AzureTarget | None:
    endpoint = env.get(f"{prefix}ENDPOINT", "")
    key = env.get(f"{prefix}API_KEY", "")
    deployment = env.get(f"{prefix}DEPLOYMENT", "")
    if not (endpoint or key):
        return None  # a deployment name alone is just the default from .env.example
    if not (endpoint and key and deployment):
        raise SettingsError(f"set all three of {prefix}ENDPOINT, {prefix}API_KEY and {prefix}DEPLOYMENT")
    if not endpoint.startswith("https://"):
        raise SettingsError(f"{prefix}ENDPOINT must start with https://")
    return AzureTarget(endpoint, key, deployment)


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8606
    trust_proxy: tuple = ()
    data_dir: Path = PROJECT_ROOT / "data"
    log_dir: Path = PROJECT_ROOT / "data" / "logs"
    log_max_bytes: int = 5 * 1024 * 1024
    run_timeout: int = 300
    runs_per_ip_per_day: int = 3
    daily_run_cap: int = 40
    daily_token_budget: int = 400000
    run_token_cap: int = 80000
    max_watchers: int = 100
    llm: str = "fake"
    primary: AzureTarget | None = None
    fallback: AzureTarget | None = None
    runner_socket: str = ""

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def relay_socket(self) -> Path:
        return self.data_dir / "sock" / "relay.sock"


def load(env: dict | None = None, env_file: Path | None = None) -> Settings:
    """The process environment wins over the .env file, as with systemd's EnvironmentFile."""
    merged = read_env_file(env_file or PROJECT_ROOT / ".env")
    merged.update(os.environ if env is None else env)
    if merged.get("HOST", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
        raise SettingsError("HOST must stay on this machine, the page is meant to sit behind nginx")
    primary = _target(merged, "AZURE_OPENAI_")
    fallback = _target(merged, "AZURE_OPENAI_FALLBACK_")
    if fallback and not primary:
        raise SettingsError("a fallback deployment needs the main AZURE_OPENAI_ settings too")
    wanted = merged.get("LIVINGCODE_WEB_LLM", "auto")
    if wanted not in ("auto", "azure", "fake"):
        raise SettingsError("LIVINGCODE_WEB_LLM must be auto, azure or fake")
    if wanted == "azure" and not primary:
        raise SettingsError("LIVINGCODE_WEB_LLM=azure needs the AZURE_OPENAI_ settings")
    llm = "fake" if wanted == "fake" or not primary else "azure"
    trust = merged.get("TRUST_PROXY", "")
    data_dir = Path(merged.get("DATA_DIR") or PROJECT_ROOT / "data")
    log_dir = Path(merged.get("LOG_DIR") or PROJECT_ROOT / "data" / "logs")
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    return Settings(
        host=merged.get("HOST", "127.0.0.1"),
        port=_int(merged, "PORT", 8606, 1),
        trust_proxy=tuple(item.strip() for item in trust.split(",") if item.strip() and item.strip() != "false"),
        data_dir=data_dir,
        log_dir=log_dir,
        log_max_bytes=_int(merged, "LOG_MAX_BYTES", 5 * 1024 * 1024, 64 * 1024),
        run_timeout=_int(merged, "RUN_TIMEOUT_SECONDS", 300, 30),
        runs_per_ip_per_day=_int(merged, "RUNS_PER_IP_PER_DAY", 3, 1),
        daily_run_cap=_int(merged, "DAILY_RUN_CAP", 40, 1),
        daily_token_budget=_int(merged, "DAILY_TOKEN_BUDGET", 400000),
        run_token_cap=_int(merged, "RUN_TOKEN_CAP", 80000, 1000),
        max_watchers=_int(merged, "MAX_WATCHERS", 100, 1),
        llm=llm,
        primary=primary,
        fallback=fallback,
        runner_socket=merged.get("LIVINGCODE_RUNNER_SOCKET", ""),
    )
