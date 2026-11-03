"""Builds a throwaway lab in a temp folder for each test class."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from livingcode import labtools  # noqa: E402
from livingcode.agent.service import Agent  # noqa: E402
from livingcode.common import config as config_module  # noqa: E402
from livingcode.guard import health  # noqa: E402
from livingcode.guard.deploy import Deployer  # noqa: E402
from livingcode.llm import FakeLLM  # noqa: E402

BUG_FIX_NEW = "if people <= 0:\n        return tinyfmt.money(0)\n    return tinyfmt.money(total / people)"
BUG_TEST = (
    "import unittest\nimport pricing\n\n\nclass ZeroPeople(unittest.TestCase):\n"
    "    def test_zero(self):\n        self.assertEqual(pricing.share(10, 0), '$0.00')\n"
)
BUG_FIX_RULES = [
    {
        "when": "ZeroDivisionError",
        "reply": {"actions": [
            {"tool": "read_file", "path": "pricing.py"},
            {"tool": "replace", "path": "pricing.py", "old": "return tinyfmt.money(total / people)", "new": BUG_FIX_NEW},
            {"tool": "write_file", "path": "tests/test_zero_people.py", "content": BUG_TEST},
            {"tool": "run_tests"},
        ]},
    },
    {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "Handle zero people in share."}]}},
]


def get(url: str, timeout: float = 3.0):
    return health.fetch(url, timeout)


class Lab:
    def __init__(self, settings: dict | None = None, enabled: bool = True):
        self.base = Path(tempfile.mkdtemp(prefix="lc-test-")).resolve()
        self.port = labtools.free_port()
        merged = labtools.merge(labtools.fast_settings(), settings or {})
        self.config_path = labtools.setup(self.base, self.port, merged)
        if enabled:
            config_module.set_enabled(self.config_path, True)
        self.reload()
        self.deployers = []
        self.url = f"http://127.0.0.1:{self.port}"

    def reload(self):
        self.cfg = config_module.load(self.config_path)
        return self.cfg

    def deployer(self, **kwargs) -> Deployer:
        deployer = Deployer(self.reload(), **kwargs)
        self.deployers.append(deployer)
        return deployer

    def agent(self, rules=None, **kwargs) -> Agent:
        return Agent(self.reload(), client=FakeLLM(rules), **kwargs)

    def start(self) -> Deployer:
        deployer = self.deployer()
        deployer.bootstrap()
        return deployer

    def set(self, **sections):
        path = self.config_path
        import tomllib

        data = tomllib.loads(path.read_text())
        path.write_text(labtools.render_toml(labtools.merge(data, sections)))
        return self.reload()

    def tree(self, mutate=None) -> Path:
        """A copy of the repo, optionally changed, for a person's deploy."""
        tree = Path(tempfile.mkdtemp(prefix="tree-", dir=self.base)) / "tree"
        shutil.copytree(self.cfg.repo, tree)
        if mutate:
            mutate(tree)
        return tree

    def candidate(self, candidate_id, changes, capability="bug_fixing", issue_id="", base=None, feature_id="", folder=None):
        """Writes a candidate by hand, the same way a hostile agent could."""
        target = (folder or self.cfg.candidates_dir) / candidate_id
        target.mkdir(parents=True)
        current = base or os.readlink(self.cfg.current_link).rsplit("/", 1)[-1]
        release = self.cfg.releases_dir / current
        items = []
        for change in changes:
            path, content = change["path"], change.get("content")
            existing = release / path
            action = change.get("action") or ("modify" if existing.is_file() else "add")
            sha = change.get("base_sha256")
            if sha is None and existing.is_file():
                sha = hashlib.sha256(existing.read_bytes()).hexdigest()
            item = {"path": path, "action": action, "base_sha256": sha}
            if action != "delete":
                item["content"] = base64.b64encode(content.encode()).decode()
            items.append(item)
        raw = json.dumps(items).encode()
        (target / "changes.json").write_bytes(raw)
        (target / "manifest.json").write_text(json.dumps({
            "id": candidate_id, "base_release": current, "capability": capability,
            "issue_id": issue_id, "summary": "test change", "feature_id": feature_id,
        }))
        return target

    def approve(self, folder: Path):
        digest = hashlib.sha256((folder / "changes.json").read_bytes()).hexdigest()
        (self.cfg.approvals_dir / f"{folder.name}.json").write_text(json.dumps({"changes_sha256": digest}))

    def proposal_to_candidate(self, candidate_id: str) -> Path:
        source = self.cfg.proposals_dir / candidate_id
        self.approve(source)
        target = self.cfg.candidates_dir / candidate_id
        os.replace(source, target)
        return target

    def trigger_bug(self):
        status, _ = get(f"{self.url}/share?total=10&people=0")
        return status

    def close(self):
        for deployer in self.deployers:
            try:
                deployer.pm.stop()
            except Exception:
                pass
            deployer.store.close()
        for name in list(logging.root.manager.loggerDict):
            if name.startswith("livingcode."):
                logger = logging.getLogger(name)
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
        time.sleep(0.05)
        shutil.rmtree(self.base, ignore_errors=True)
