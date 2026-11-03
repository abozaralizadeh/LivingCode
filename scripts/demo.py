"""Runs the whole lab story once, on a fresh temp lab, and prints what happens.

A visitor hits a bug, the agent (the local fake LLM) writes a fix and a test, a person
approves it, the guard deploys it, a bad version from a person gets rolled back on its
own, and then a rollback runs with the network blocked by the operating system.

Run it with: python3 scripts/demo.py
Nothing here talks to the internet or a real LLM.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from livingcode import cli, labtools  # noqa: E402
from livingcode.common import config as config_module  # noqa: E402

FIX_NEW = "if people <= 0:\n        return tinyfmt.money(0)\n    return tinyfmt.money(total / people)"
FIX_TEST = (
    "import unittest\nimport pricing\n\n\nclass ZeroPeople(unittest.TestCase):\n"
    "    def test_zero(self):\n        self.assertEqual(pricing.share(10, 0), '$0.00')\n"
)
FAKE_LLM_RULES = [
    {
        "when": "ZeroDivisionError",
        "reply": {"actions": [
            {"tool": "read_file", "path": "pricing.py"},
            {"tool": "replace", "path": "pricing.py", "old": "return tinyfmt.money(total / people)", "new": FIX_NEW},
            {"tool": "write_file", "path": "tests/test_zero_people.py", "content": FIX_TEST},
            {"tool": "run_tests"},
        ]},
    },
    {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "Handle zero people in share."}]}},
]
NO_NETWORK = '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'


class Demo:
    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="lc-demo-")).resolve()
        self.port = labtools.free_port()
        self.config_path = labtools.setup(self.base, self.port, labtools.fast_settings())
        self.url = f"http://127.0.0.1:{self.port}"

    def say(self, text: str) -> None:
        print(f"\n== {text}")

    def tidy(self, text: str) -> str:
        return text.replace(str(self.base), "<lab>").rstrip()

    def cli(self, *args: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main(["--config", str(self.config_path), *args])
        text = self.tidy(output.getvalue())
        print(self.tidy(f"$ livingcode {' '.join(args)}"))
        if text:
            print("  " + text.replace("\n", "\n  "))
        if code:
            print(f"  (exit {code})")
        return text

    def visit(self, path: str) -> int:
        try:
            with urllib.request.urlopen(f"{self.url}{path}", timeout=3) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        print(f"  visitor GET {path} -> {status}")
        return status

    def wait_for_error_in_log(self) -> None:
        log = Path(config_module.load(self.config_path).app.log_files[0])
        for _ in range(100):
            if log.exists() and "ZeroDivisionError" in log.read_text():
                return
            time.sleep(0.05)
        raise SystemExit("the error never reached the app log")

    def bad_tree(self) -> Path:
        """A person's change where the checkout page breaks. Its tests still pass."""
        cfg = config_module.load(self.config_path)
        tree = self.base / "people-change"
        shutil.copytree(cfg.repo, tree)
        routes = tree / "routes.py"
        routes.write_text(routes.read_text().replace('return 200, "checkout"', 'return 500, "checkout"'))
        return tree

    def rollback_without_network(self) -> str:
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"), "HOME": str(self.base),
               "TMPDIR": tempfile.gettempdir()}
        done = subprocess.run(
            ["sandbox-exec", "-p", NO_NETWORK, sys.executable, "-m", "livingcode",
             "--config", str(self.config_path), "rollback"],
            env=env, capture_output=True, text=True, timeout=120,
        )
        print("$ sandbox-exec (no network) livingcode rollback")
        print("  " + self.tidy(done.stdout + done.stderr).replace("\n", "\n  "))
        return done.stdout

    def run(self) -> None:
        started = time.time()
        print("LivingCode lab demo. Local fake LLM, throwaway sample app, nothing leaves this Mac.")

        self.say("1. Install the sample app and check everything before turning anything on")
        self.cli("install")
        self.cli("doctor")
        self.cli("on")
        self.cli("status")

        self.say("2. A visitor hits a bug in the sample shop")
        self.visit("/share?total=10&people=0")
        self.wait_for_error_in_log()

        self.say("3. The agent reads the error and writes a fix and a test (bug fixing is on propose)")
        Path(config_module.load(self.config_path).llm.fake_script).write_text(json.dumps(FAKE_LLM_RULES))
        candidate = self.cli("run").split("Candidate: ")[-1].strip()
        self.cli("proposals")
        self.cli("show", candidate)

        self.say("4. A person approves it, and only the guard deploys it")
        self.cli("approve", candidate)
        self.visit("/share?total=10&people=0")

        self.say("5. A person deploys a version that breaks the checkout page. Its tests pass, the smoke check doesn't")
        self.cli("deploy", str(self.bad_tree()))
        self.visit("/checkout")

        self.say("6. Roll back to the release before the fix, with the network blocked by macOS")
        self.rollback_without_network()
        self.visit("/share?total=10&people=0")

        self.say("7. What happened, and what it cost")
        self.cli("history")
        self.cli("cost")
        print("  (the fake LLM costs nothing. It reports rough token counts so the budget code still gets used)")
        sent = config_module.load(self.config_path).agent_dir / "sent-to-llm.jsonl"
        lines = sent.read_text().splitlines() if sent.exists() else []
        print(f"  {len(lines)} LLM calls are written down in sent-to-llm.jsonl, after redaction")
        self.cli("off")
        print(f"\nDone in {time.time() - started:.0f} seconds.")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.cli("lab", "stop")
        shutil.rmtree(self.base, ignore_errors=True)


def main() -> int:
    demo = Demo()
    try:
        demo.run()
    finally:
        demo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
