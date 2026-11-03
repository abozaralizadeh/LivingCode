"""The agent's copy of the project, and the short list of tools the LLM can use on it.

There's no shell tool. Paths the guard would refuse are refused here too, so the LLM gets
told early, but the guard's own check is the one that counts.
"""
from __future__ import annotations

import base64
import hashlib
import re
import shutil
import sys
from pathlib import Path

from ..common.pathrules import is_clean, is_existing_test, is_protected, is_sensitive

MAX_READ = 60000
MAX_SEARCH_RESULTS = 40
SKIP_DIRS = {"_deps", "__pycache__", ".git", ".tmp"}


class ToolRefused(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Workspace:
    def __init__(self, cfg, base: Path, folder: Path, sandbox):
        self.cfg = cfg
        self.base = base
        self.root = folder
        self.sandbox = sandbox
        shutil.rmtree(folder, ignore_errors=True)
        folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base, folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), symlinks=True)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _target(self, path: str) -> Path:
        if not isinstance(path, str) or not is_clean(path):
            raise ToolRefused(f"unsafe path: {path!r}")
        target = self.root / path
        if target.is_symlink() or self.root.resolve() not in target.resolve().parents:
            raise ToolRefused(f"path leaves the project: {path}")
        return target

    def files(self) -> list[str]:
        found = []
        for item in sorted(self.root.rglob("*")):
            parts = item.relative_to(self.root).parts
            if item.is_file() and not SKIP_DIRS & set(parts):
                found.append("/".join(parts))
        return found

    def read_file(self, path: str) -> str:
        target = self._target(path)
        if is_sensitive(path):
            raise ToolRefused(f"sensitive files are never read: {path}")
        if not target.is_file():
            raise ToolRefused(f"no such file: {path}")
        return target.read_text(errors="replace")[:MAX_READ]

    def search(self, pattern: str) -> str:
        try:
            expression = re.compile(pattern)
        except (re.error, TypeError) as exc:
            raise ToolRefused(f"bad search pattern: {exc}") from exc
        results = []
        for name in self.files():
            if is_sensitive(name):
                continue
            for number, line in enumerate((self.root / name).read_text(errors="replace").splitlines(), 1):
                if expression.search(line):
                    results.append(f"{name}:{number}: {line.strip()[:200]}")
                    if len(results) >= MAX_SEARCH_RESULTS:
                        return "\n".join(results)
        return "\n".join(results) or "no matches"

    def _editable(self, path: str) -> Path:
        target = self._target(path)
        if is_sensitive(path) or is_protected(path, self.cfg.safety):
            raise ToolRefused(f"protected path, the guard would refuse this: {path}")
        if is_existing_test(path, self.cfg.safety) and target.exists():
            raise ToolRefused(f"existing tests can't be edited: {path}")
        return target

    def write_file(self, path: str, content: str) -> str:
        target = self._editable(path)
        if not isinstance(content, str) or "[REDACTED" in content:
            raise ToolRefused("content is missing or contains a redaction marker")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path}"

    def replace(self, path: str, old: str, new: str) -> str:
        target = self._editable(path)
        if not target.is_file():
            raise ToolRefused(f"no such file: {path}")
        text = target.read_text()
        if not isinstance(old, str) or not isinstance(new, str) or "[REDACTED" in new:
            raise ToolRefused("old and new must be text, without redaction markers")
        if text.count(old) != 1:
            raise ToolRefused(f"old text must appear exactly once, found {text.count(old)}")
        target.write_text(text.replace(old, new))
        return f"edited {path}"

    def run_tests(self) -> tuple[bool, str]:
        command = [part.replace("{python}", sys.executable) for part in self.cfg.app.test_command]
        result = self.sandbox.run(self.root, command, self.cfg.app.test_timeout_seconds)
        return result.code == 0, result.output[-3000:]

    def changes(self) -> list[dict]:
        """Everything that differs from the base release, in the candidate format."""
        before = {}
        for item in self.base.rglob("*"):
            parts = item.relative_to(self.base).parts
            if item.is_file() and not SKIP_DIRS & set(parts):
                before["/".join(parts)] = item
        after = set(self.files())
        found = []
        for name in sorted(after):
            data = (self.root / name).read_bytes()
            entry = {"path": name, "content": base64.b64encode(data).decode()}
            if name not in before:
                found.append({**entry, "action": "add", "base_sha256": None})
            elif _sha(before[name]) != hashlib.sha256(data).hexdigest():
                found.append({**entry, "action": "modify", "base_sha256": _sha(before[name])})
        for name in sorted(set(before) - after):
            found.append({"path": name, "action": "delete", "base_sha256": _sha(before[name])})
        return found
