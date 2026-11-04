"""One web demo run: a fresh lab, a planted bug, the agent and the guard. Then it's all deleted.

It reads one JSON line, {"scenario": "<id>"}, and writes one JSON event per line. On the VM
systemd starts it once per run, in a unit with no network, with the socket as stdin and
stdout. Locally the front starts it as a child process. Run it by hand with:

    echo '{"scenario": "zero-people"}' | PYTHONPATH=src python3 -m livingcode.webdemo.runner

Without LIVINGCODE_RELAY_SOCKET it uses the scenario's local fake LLM script.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import cli, labtools
from ..common import config as config_module
from .scenarios import SCENARIOS

RELAY_ENV = "LIVINGCODE_RELAY_SOCKET"
DEMO_LLM = {"max_turns": 8, "max_tries": 3, "max_tokens": 6000}


class Gone(Exception):
    """The front stopped listening, so there's no point going on."""


def describe(action: dict) -> str:
    tool = action.get("tool")
    path = str(action.get("path", ""))[:120]
    if tool == "read_file":
        return f"reads {path}"
    if tool == "search":
        return f"searches for {str(action.get('pattern', ''))[:80]!r}"
    if tool == "write_file":
        return f"writes {path}"
    if tool == "replace":
        return f"edits {path}"
    if tool == "run_tests":
        return "runs the tests in the sandbox"
    if tool == "finish":
        return f"says it's done: {str(action.get('summary', ''))[:200]}"
    if tool == "give_up":
        return f"gives up: {str(action.get('reason', ''))[:200]}"
    return f"asks for a tool that doesn't exist: {str(tool)[:40]}"


class Narrator:
    """Wraps the agent's LLM client so the page can see each turn as it happens."""

    def __init__(self, client, say):
        self.client = client
        self.say = say
        self.turn = 0
        self.tokens = 0

    def complete(self, system: str, messages: list[dict], max_tokens: int):
        self.turn += 1
        self.say(f"turn {self.turn}: asking the AI")
        reply = self.client.complete(system, messages, max_tokens)
        self.tokens += reply.input_tokens + reply.output_tokens
        try:
            data = json.loads(reply.text[reply.text.find("{"): reply.text.rfind("}") + 1])
            actions = data.get("actions") or []
        except (ValueError, AttributeError):
            self.say("the reply wasn't one JSON object, so the agent asks again")
            return reply
        thought = str(data.get("thought", "")).strip()
        if thought:
            self.say(f"thinks: {thought[:240]}")
        for action in actions[:12]:
            if isinstance(action, dict):
                self.say(describe(action))
        return reply


class Run:
    def __init__(self, scenario, out, relay_socket: str = ""):
        self.scenario = scenario
        self.out = out
        self.base = Path(tempfile.mkdtemp(prefix="lc-web-")).resolve()
        self.port = labtools.free_port()
        llm = {"provider": "relay", "model": "relay", "socket": relay_socket} if relay_socket else {}
        settings = labtools.merge(labtools.fast_settings(), {"llm": {**DEMO_LLM, **llm}})
        self.config_path = labtools.setup(self.base, self.port, settings)
        self.cfg = config_module.load(self.config_path)
        if not relay_socket:
            Path(self.cfg.llm.fake_script).write_text(json.dumps(scenario.fake_rules))
        if scenario.plant:
            scenario.plant(self.cfg.repo)
        self.url = f"http://127.0.0.1:{self.port}"

    def emit(self, kind: str, **fields) -> None:
        try:
            self.out.write(json.dumps({"t": kind, **fields}) + "\n")
            self.out.flush()
        except (OSError, ValueError):
            raise Gone() from None

    def tidy(self, text: str) -> str:
        return text.replace(str(self.base), "<lab>").replace(f"127.0.0.1:{self.port}", "127.0.0.1:<port>").rstrip()

    def step(self, text: str) -> None:
        self.emit("step", text=text)

    def line(self, text: str) -> None:
        self.emit("line", text=self.tidy(text))

    def ai(self, text: str) -> None:
        self.emit("ai", text=self.tidy(text))

    def cli(self, *args: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main(["--config", str(self.config_path), *args])
        text = self.tidy(output.getvalue())
        shown = [self.tidy(arg) for arg in args]
        self.line(f"$ livingcode {' '.join(shown)}")
        for row in text.splitlines():
            self.line(f"  {row}")
        if code:
            self.line(f"  (exit {code})")
        self.last_code = code
        return text

    def visit(self, path: str) -> int:
        try:
            with urllib.request.urlopen(f"{self.url}{path}", timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except OSError:
            status = 0
        self.line(f"  visitor GET {path} -> {status or 'no answer'}")
        return status

    def wait_for_error(self) -> bool:
        log = Path(self.cfg.app.log_files[0])
        for _ in range(100):
            if log.exists() and "request failed" in log.read_text(errors="replace"):
                return True
            time.sleep(0.05)
        return False

    def broken_checkout(self) -> Path:
        """A person's change to the live release where the checkout page breaks. Its tests pass."""
        live = self.cfg.releases_dir / os.readlink(self.cfg.current_link).rsplit("/", 1)[-1]
        tree = self.base / "people-change"
        shutil.copytree(live, tree, ignore=shutil.ignore_patterns("_deps", "__pycache__"))
        routes = tree / "routes.py"
        routes.write_text(routes.read_text().replace('return 200, "checkout"', 'return 500, "checkout"'))
        return tree

    def run(self) -> None:
        from ..agent.service import Agent
        from ..llm import make_client

        scenario = self.scenario
        self.step("1. A fresh lab: install the sample shop and check everything first")
        for command in ("install", "doctor", "on"):
            self.cli(command)
            if self.last_code:
                self.emit("done", ok=False, summary=f"The fresh lab didn't pass its checks (livingcode {command} failed), "
                          "so the run stopped before the AI was asked anything.")
                return
        self.cfg = config_module.load(self.config_path)

        self.step(f"2. A visitor hits a bug: {scenario.story}")
        self.visit(scenario.path)
        if not self.wait_for_error():
            self.emit("done", ok=False, summary="The bug never showed up in the app log, so there was nothing to fix.")
            return

        self.step("3. The agent reads the error and asks the AI for a fix and a test")
        narrator = Narrator(make_client(self.cfg), self.ai)
        result = Agent(self.cfg, client=narrator).run_round("web")
        self.line(f"  {result.status}: {result.detail}")
        if not result.candidate_id:
            self.emit("done", ok=False, tokens=narrator.tokens,
                      summary=f"The agent didn't find a fix this time ({result.detail[:200]}). Nothing was deployed.")
            return

        self.step("4. The change, the way a person reviews it")
        self.cli("show", result.candidate_id)

        self.step("5. The page approves it for you. Only the guard deploys it, and it watches it")
        outcome = self.cli("approve", result.candidate_id)
        status = self.visit(scenario.path)
        fixed = outcome.startswith("Settled") and 0 < status < 500

        self.step("6. Then a person ships a broken checkout page. Its tests pass, the smoke check doesn't")
        self.cli("deploy", str(self.broken_checkout()))
        self.visit("/checkout")

        self.step("7. What happened")
        self.cli("history")
        if fixed:
            summary = f"Fixed and live: {result.detail[:200]} The broken checkout was rolled back on its own."
        else:
            summary = "The fix didn't hold, so the guard kept the last good version. The broken checkout was rolled back on its own."
        self.emit("done", ok=fixed, tokens=narrator.tokens, summary=summary)

    def close(self) -> None:
        with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--config", str(self.config_path), "lab", "stop"])
        shutil.rmtree(self.base, ignore_errors=True)


def _stop(*_args) -> None:
    raise SystemExit(1)


def main() -> int:
    # A stop from the front or from systemd still runs the clean up below.
    signal.signal(signal.SIGTERM, _stop)
    out = sys.stdout
    try:
        request = json.loads(sys.stdin.readline() or "{}")
    except ValueError:
        request = {}
    scenario = SCENARIOS.get(str(request.get("scenario", "")))
    if scenario is None:
        out.write(json.dumps({"t": "done", "ok": False, "summary": "Unknown scenario."}) + "\n")
        return 2
    run = Run(scenario, out, os.environ.get(RELAY_ENV, ""))
    try:
        run.run()
    except Gone:
        return 1
    except Exception as exc:  # the page should always get an ending
        print(f"run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        with contextlib.suppress(Gone):
            run.emit("done", ok=False, summary=f"The run stopped on an error: {type(exc).__name__}.")
        return 1
    finally:
        run.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
