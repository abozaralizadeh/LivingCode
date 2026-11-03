"""Fixed rules about which paths may change. Used by the agent's tools and, separately
enforced, by the guard. The guard's check is the one that counts."""
from __future__ import annotations

import fnmatch
import unicodedata
from pathlib import PurePosixPath

ALWAYS_PROTECTED = (
    ".env", ".git/", "_deps/", "livingcode/", "lab/", "deploy/", "conftest.py", "pytest.ini",
    "setup.cfg", "tox.ini", "pyproject.toml", "setup_service.sh", "Dockerfile",
    "LIVINGCODE_THROWAWAY_SAMPLE",
)
SENSITIVE_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "secrets*", "credentials*", ".netrc", ".npmrc", ".pypirc",
)
LOCK_FILE = "requirements.lock"


def normalize(path: str) -> str:
    # macOS file names ignore case and accents differ by form, so compare a folded version.
    return unicodedata.normalize("NFC", path).replace("\\", "/").casefold()


def is_clean(path: str) -> bool:
    """A relative path with no tricks in it."""
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and not any(part in ("..", ".") for part in path.split("/"))


def _matches(path: str, rule: str) -> bool:
    path, rule = normalize(path), normalize(rule)
    parts = path.split("/")
    if rule.endswith("/"):
        folder = rule.rstrip("/")
        if "/" in folder:
            return path.startswith(rule)
        return folder in parts[:-1]
    if "/" in rule:
        return path == rule
    return parts[-1] == rule or parts[-1].startswith(rule + ".") and rule == ".env"


def is_protected(path: str, safety) -> bool:
    rules = ALWAYS_PROTECTED + tuple(safety.protected_paths) + tuple(safety.health_paths)
    return any(_matches(path, rule) for rule in rules)


def is_sensitive(path: str) -> bool:
    name = normalize(path).split("/")[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAMES)


def is_existing_test(path: str, safety) -> bool:
    return normalize(path).startswith(normalize(safety.tests_path))
