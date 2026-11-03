"""Fixed code checks. The built-in scanner is plain rules over the parsed code. The bandit
adapter is for later, once it's installed, and is tested with a fake command runner."""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..common import runner as default_runner

SECRET = re.compile(r"""(?i)(password|secret|api[_-]?key|token)\s*=\s*["'][^"'\s]{8,}["']""")
RISKY_CALLS = {
    "eval": "eval", "exec": "exec", "compile": "compile", "__import__": "dynamic-import",
    "os.system": "os-system", "os.popen": "os-popen", "pickle.loads": "pickle",
    "pickle.load": "pickle", "marshal.loads": "marshal", "subprocess.getoutput": "shell",
    "subprocess.getstatusoutput": "shell",
}
RISKY_IMPORTS = {"socket", "ctypes", "urllib.request", "http.client", "ftplib", "telnetlib", "requests"}


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    severity: str = "high"


def _call_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def scan_source(path: str, text: str) -> list[Finding]:
    found = []
    if SECRET.search(text):
        found.append(Finding(path, "hardcoded-secret"))
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return found + [Finding(path, "syntax-error")]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in RISKY_CALLS:
                found.append(Finding(path, RISKY_CALLS[name]))
            elif name.startswith("subprocess.") and any(
                kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value
                for kw in node.keywords
            ):
                found.append(Finding(path, "shell-true"))
        elif isinstance(node, ast.Import):
            found += [Finding(path, f"import-{a.name}") for a in node.names if a.name in RISKY_IMPORTS]
        elif isinstance(node, ast.ImportFrom) and node.module in RISKY_IMPORTS:
            found.append(Finding(path, f"import-{node.module}"))
    return found


class BuiltinScanner:
    def scan(self, tree: Path, paths: list[str]) -> list[Finding]:
        found = []
        for name in paths:
            target = tree / name
            if name.endswith(".py") and target.is_file():
                found += scan_source(name, target.read_text(errors="replace"))
        return found


class BanditScanner:
    def __init__(self, runner=default_runner.run):
        self.runner = runner

    def scan(self, tree: Path, paths: list[str]) -> list[Finding]:
        files = [name for name in paths if name.endswith(".py") and (tree / name).is_file()]
        if not files:
            return []
        done = self.runner(["bandit", "-f", "json", "-q", *files], cwd=tree, timeout=120)
        try:
            results = json.loads(done.output).get("results", [])
        except ValueError as exc:
            raise RuntimeError(f"bandit gave no usable result: {done.output[:300]}") from exc
        return [
            Finding(item["filename"].removeprefix("./"), item["test_id"], item["issue_severity"].lower())
            for item in results
        ]


def new_high_findings(scanner, before: Path, after: Path, paths: list[str]) -> list[Finding]:
    """Findings at high severity that weren't already there before the change."""
    existing = list(scanner.scan(before, paths))
    fresh = []
    for finding in scanner.scan(after, paths):
        if finding in existing:
            existing.remove(finding)
        elif finding.severity == "high":
            fresh.append(finding)
    return fresh
