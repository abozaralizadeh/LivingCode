"""Builds the lab: a throwaway copy of the sample app, with local stand-ins for everything
outside it (LLM, package index, advisories, git remote)."""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
import time
from pathlib import Path

from .common.lab import PROJECT_ROOT
from .common import config as config_module

SAMPLE_APP = PROJECT_ROOT / "sample_app"
SAMPLE_INDEX = PROJECT_ROOT / "sample_index"
DEFAULT_LAB = PROJECT_ROOT / "lab"
DEFAULT_PORT = 18080
# Days since each version of the fake dependency was published.
VERSION_AGES = {"1.0.0": 60, "1.0.1": 30, "1.1.0": 20, "1.2.0": 1, "2.0.0": 15}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"can't write {value!r} as TOML")


def render_toml(data: dict) -> str:
    lines = []
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines += ["", f"[{section}]"]
        nested = {k: v for k, v in values.items() if isinstance(v, dict)}
        lines += [f"{k} = {_toml_value(v)}" for k, v in values.items() if k not in nested]
        for name, table in nested.items():
            lines += ["", f"[{section}.{name}]"]
            lines += [f"{k} = {_toml_value(v)}" for k, v in table.items()]
    return "\n".join(lines) + "\n"


def lab_settings(base: Path, port: int) -> dict:
    root = base / "opt" / "sample"
    log_dir = base / "var" / "log"
    log_file = log_dir / "sample.log"
    home = f"http://127.0.0.1:{port}"
    return {
        "enabled": False,
        "app": {
            "name": "sample", "repo": str(root / "repo"), "root": str(root), "runner": "local",
            "log_files": [str(log_file)], "env": {"PORT": str(port), "LOG_FILE": str(log_file)},
        },
        "paths": {"state_dir": str(base / "var" / "lib"), "log_dir": str(log_dir)},
        "llm": {"provider": "fake", "model": "fake", "fake_script": str(base / "etc" / "fake_llm.json")},
        "capabilities": {
            "bug_fixing": "propose", "updating": "off", "fixing_vulnerabilities": "off",
            "watching_user_behaviour": "off", "adding_features": "off",
        },
        "health": {
            "url": f"{home}/health",
            "smoke_checks": [
                {"url": f"{home}/", "expect_status": 200, "expect_text": "Sample shop"},
                {"url": f"{home}/checkout", "expect_status": 200},
            ],
        },
        "safety": {"protected_paths": ["migrations/", "auth/", "payments/"]},
        "sources": {
            "package_index": str(base / "index"),
            "registry_file": str(base / "etc" / "registry.json"),
            "advisories_file": str(base / "etc" / "advisories.json"),
            "features_file": str(base / "etc" / "features.toml"),
        },
    }


def fast_settings() -> dict:
    """Short timings for tests and the demo. Real defaults are in the config dataclasses."""
    return {
        "health": {
            "startup_seconds": 5.0, "ok_count": 2, "window_seconds": 5.0, "interval_seconds": 0.05,
            "probation_minutes": 0.01, "poll_seconds": 0.1, "min_errors": 3,
        },
        "limits": {"deploys_per_day": 1000, "rounds_per_day": 1000, "rollback_wait_hours": 0.0},
    }


def merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def registry_data() -> dict:
    now = time.time()
    entries = []
    for version, days in VERSION_AGES.items():
        data = (SAMPLE_INDEX / "tinyfmt" / version / "tinyfmt.py").read_bytes()
        entries.append({
            "version": version, "published_at": now - days * 86400,
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    return {"tinyfmt": entries}


def setup(base: Path, port: int = DEFAULT_PORT, settings: dict | None = None) -> Path:
    """Creates a fresh lab folder and returns the path of its config file."""
    base = Path(base).resolve()
    for folder in ("etc", "opt/sample", "var/log", "var/lib"):
        (base / folder).mkdir(parents=True, exist_ok=True)
    repo = base / "opt" / "sample" / "repo"
    shutil.rmtree(repo, ignore_errors=True)
    shutil.copytree(SAMPLE_APP, repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.rmtree(base / "index", ignore_errors=True)
    shutil.copytree(SAMPLE_INDEX, base / "index")
    etc = base / "etc"
    (etc / "registry.json").write_text(json.dumps(registry_data(), indent=2))
    (etc / "advisories.json").write_text(json.dumps([
        {"id": "LAB-2026-0001", "package": "tinyfmt", "affected": ["1.0.0"], "fixed": "1.0.1", "severity": "high"},
    ], indent=2))
    (etc / "features.toml").write_text(
        '[[feature]]\nid = "price-page"\ntitle = "Show a prices page"\n'
        'description = "Add a /prices page that lists the three plan prices."\n'
    )
    (etc / "fake_llm.json").write_text("[]\n")
    data = merge(lab_settings(base, port), settings or {})
    path = etc / "sample.toml"
    path.write_text(render_toml(data))
    config_module.load(path)
    return path
