"""The rules that protect the core idea: the safety net isn't AI, and the lab stays a lab."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tests.harness import ROOT, Lab

SRC = ROOT / "src" / "livingcode"
GUARD = SRC / "guard"


def imports_in(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
    return found


class GuardHasNoAI(unittest.TestCase):
    def test_guard_never_imports_the_llm_or_agent(self):
        for path in list(GUARD.glob("*.py")) + list((SRC / "common").glob("*.py")):
            for name in imports_in(path):
                with self.subTest(file=path.name, name=name):
                    self.assertNotRegex(name, r"(^|\.)(llm|agent|anthropic|openai)(\.|$)")

    def test_importing_the_guard_doesnt_load_the_llm_code(self):
        code = (
            "import sys; sys.path.insert(0, 'src')\n"
            "import livingcode.guard.deploy, livingcode.guard.service\n"
            "bad = [m for m in sys.modules if '.llm' in m or '.agent' in m]\n"
            "print(bad); sys.exit(1 if bad else 0)\n"
        )
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_guard_only_uses_the_standard_library(self):
        allowed = set(sys.stdlib_module_names)
        for path in list(GUARD.glob("*.py")) + list((SRC / "common").glob("*.py")):
            for name in imports_in(path):
                if not name.startswith("."):
                    self.assertIn(name.split(".")[0], allowed, f"{path.name}: {name}")

    def test_guard_stays_small(self):
        lines = sum(len(path.read_text().splitlines()) for path in GUARD.glob("*.py"))
        self.assertLess(lines, 2000)

    def test_rollback_needs_no_network_and_no_key(self):
        """Runs a real rollback with the operating system blocking everything but this machine."""
        lab = Lab()
        self.addCleanup(lab.close)
        guard = lab.start()
        live = guard.releases.current()
        tree = lab.tree(lambda t: (t / "health.py").write_text("def status():\n    return 500, 'x'\n"))
        script = (
            "import sys\nfrom livingcode.common import config\n"
            "from livingcode.guard.deploy import Deployer\n"
            "print(Deployer(config.load(sys.argv[1])).deploy_ref(sys.argv[2]).state)\n"
        )
        profile = '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC.parent), "HOME": str(lab.base),
               "TMPDIR": tempfile.gettempdir()}
        done = subprocess.run(
            ["sandbox-exec", "-p", profile, sys.executable, "-c", script, str(lab.config_path), str(tree)],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(done.stdout.strip(), "RolledBack", done.stderr[-800:])
        self.assertEqual(guard.releases.current(), live)
        guard.pm.restart()


class LabLock(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(enabled=False)
        self.addCleanup(self.lab.close)

    def assert_refused(self, reason):
        from livingcode.common.lab import LabLockError, check
        from livingcode.guard.deploy import Deployer

        with self.assertRaisesRegex(LabLockError, reason):
            check(self.lab.reload())
        with self.assertRaises(LabLockError):
            Deployer(self.lab.cfg)

    def test_project_without_the_marker_is_refused(self):
        (self.lab.cfg.repo / "LIVINGCODE_THROWAWAY_SAMPLE").unlink()
        self.assert_refused("marker")

    def test_real_paths_are_refused(self):
        self.lab.set(app={"root": "/opt/real-app"})
        self.assert_refused("outside the lab")

    def test_remote_health_url_is_refused(self):
        self.lab.set(health={"url": "https://example.com/health"})
        self.assert_refused("stay on this machine")

    def test_real_llm_host_is_refused(self):
        self.lab.set(llm={"provider": "anthropic"})
        self.assert_refused("real LLM hosts")
        self.lab.set(llm={"provider": "openai", "base_url": "https://api.openai.com"})
        self.assert_refused("real LLM hosts")

    def test_remote_webhook_is_refused(self):
        self.lab.set(safety={"alert_webhook": "https://hooks.example.com/x"})
        self.assert_refused("webhook")

    def test_git_push_is_refused(self):
        self.lab.set(sync={"backend": "git", "push": True})
        self.assert_refused("real remote")

    def test_other_machines_are_refused(self):
        with mock.patch("platform.system", return_value="Linux"):
            self.assert_refused("only allows this Mac")

    def test_the_agent_checks_the_lock_too(self):
        from livingcode.agent.service import Agent
        from livingcode.common.lab import LabLockError

        (self.lab.cfg.repo / "LIVINGCODE_THROWAWAY_SAMPLE").unlink()
        with self.assertRaises(LabLockError):
            Agent(self.lab.cfg, client=object())


class Sandbox(unittest.TestCase):
    def setUp(self):
        from livingcode.common.sandbox import MacSandbox

        self.sandbox = MacSandbox()
        self.tree = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tree)]))

    def run_code(self, code, timeout=20):
        return self.sandbox.run(self.tree, [sys.executable, "-c", code], timeout)

    def test_no_network(self):
        result = self.run_code("import socket\nsocket.create_connection(('1.1.1.1', 53), 2)")
        self.assertNotEqual(result.code, 0)
        self.assertIn("not permitted", result.output)

    def test_no_reading_the_home_folder(self):
        target = Path.home() / ".zshrc"
        result = self.run_code(f"print(open({str(target)!r}).read())")
        self.assertNotEqual(result.code, 0)

    def test_no_writing_outside(self):
        result = self.run_code("open('/tmp/lc-sandbox-escape', 'w').write('x')")
        self.assertNotEqual(result.code, 0)
        self.assertFalse(Path("/tmp/lc-sandbox-escape").exists())

    def test_no_secrets_in_the_environment(self):
        with mock.patch.dict(os.environ, {"LIVINGCODE_LLM_API_KEY": "test-value"}):
            result = self.run_code("import os\nprint(sorted(os.environ))")
        self.assertNotIn("LIVINGCODE_LLM_API_KEY", result.output)

    def test_changes_dont_reach_the_original(self):
        self.assertEqual(self.run_code("open('made.txt', 'w').write('x')").code, 0)
        self.assertFalse((self.tree / "made.txt").exists())

    def test_timeout_kills_it(self):
        result = self.run_code("import time\ntime.sleep(30)", timeout=1)
        self.assertTrue(result.timed_out)


class FakeProvider(BaseHTTPRequestHandler):
    seen = []
    reply = {}
    fail_first = 0

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        headers = {key.lower(): value for key, value in self.headers.items()}
        FakeProvider.seen.append({"path": self.path, "headers": headers, "body": body})
        if FakeProvider.fail_first > 0:
            FakeProvider.fail_first -= 1
            self.send_response(429)
            self.send_header("retry-after", "0")
            self.end_headers()
            return
        data = json.dumps(FakeProvider.reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Adapters(unittest.TestCase):
    """Real adapter code against a local server. No real provider is ever contacted."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        FakeProvider.seen = []
        FakeProvider.fail_first = 0

    def test_anthropic_request_and_reply(self):
        from livingcode.llm.anthropic import AnthropicClient

        FakeProvider.reply = {
            "model": "test", "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }
        reply = AnthropicClient("test-key", "test", self.base).complete("sys", [{"role": "user", "content": "hi"}], 100)
        self.assertEqual((reply.text, reply.input_tokens, reply.output_tokens), ("hello", 7, 3))
        sent = FakeProvider.seen[0]
        self.assertEqual(sent["path"], "/v1/messages")
        self.assertEqual(sent["headers"]["x-api-key"], "test-key")
        self.assertEqual(sent["body"]["system"], "sys")

    def test_anthropic_refusal_is_an_error(self):
        from livingcode.llm import LLMError
        from livingcode.llm.anthropic import AnthropicClient

        FakeProvider.reply = {"stop_reason": "refusal", "content": [], "usage": {}}
        with self.assertRaises(LLMError):
            AnthropicClient("test-key", "test", self.base).complete("s", [{"role": "user", "content": "x"}], 10)

    def test_openai_request_and_reply(self):
        from livingcode.llm.openai import OpenAIClient

        FakeProvider.reply = {
            "model": "test", "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2},
        }
        reply = OpenAIClient("test-key", "test", self.base).complete("sys", [{"role": "user", "content": "hi"}], 100)
        self.assertEqual((reply.text, reply.input_tokens, reply.output_tokens), ("hello", 9, 2))
        sent = FakeProvider.seen[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["authorization"], "Bearer test-key")
        self.assertEqual(sent["body"]["messages"][0], {"role": "system", "content": "sys"})

    def test_retries_on_rate_limit_and_never_shows_the_key(self):
        from livingcode.llm import LLMError
        from livingcode.llm.base import post_json

        FakeProvider.reply = {"ok": True}
        FakeProvider.fail_first = 1
        self.assertEqual(post_json(f"{self.base}/x", {}, {}), {"ok": True})
        FakeProvider.fail_first = 5
        with self.assertRaises(LLMError) as caught:
            post_json(f"{self.base}/x", {"x-api-key": "test-key"}, {}, retries=1)
        self.assertNotIn("test-key", str(caught.exception))

    def test_lab_config_can_use_a_local_provider(self):
        from livingcode import llm

        lab = Lab(enabled=False, settings={"llm": {"provider": "anthropic", "base_url": self.base}})
        self.addCleanup(lab.close)
        with mock.patch.dict(os.environ, {"LIVINGCODE_LLM_API_KEY": "test-key"}):
            self.assertEqual(llm.make_client(lab.cfg).base_url, self.base)
        with mock.patch.dict(os.environ, {"LIVINGCODE_LLM_API_KEY": ""}):
            with self.assertRaises(llm.LLMError):
                llm.make_client(lab.cfg)

    def test_pypi_registry_reads_the_json_shape(self):
        from livingcode.agent.registry import PypiRegistry

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps({"releases": {"1.0": [{
                    "packagetype": "bdist_wheel", "yanked": False,
                    "upload_time_iso_8601": "2026-01-02T03:04:05Z", "digests": {"sha256": "ab"},
                }], "1.1": []}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        versions = PypiRegistry(f"http://127.0.0.1:{server.server_port}").versions("x")
        self.assertEqual([v.version for v in versions], ["1.0"])


class CommandAdapters(unittest.TestCase):
    """Adapters that can't run on this Mac. A fake runner checks the commands they build.
    None of these commands has ever really run, so they prove the shape and nothing more."""

    def setUp(self):
        self.commands = []

    def fake(self, output="", code=0):
        from livingcode.common.runner import Result

        def runner(command, **kwargs):
            self.commands.append(command)
            return Result(code, output)
        return runner

    def lab(self):
        lab = Lab(enabled=False, settings={"app": {"service": "sample.service"}})
        self.addCleanup(lab.close)
        return lab

    def test_systemd_manager(self):
        from livingcode.guard.procman import SystemdManager

        manager = SystemdManager(self.lab().cfg, self.fake())
        manager.restart()
        self.assertTrue(manager.is_active())
        self.assertEqual(self.commands[0], ["sudo", "-n", "systemctl", "restart", "sample.service"])
        self.assertEqual(self.commands[1], ["systemctl", "is-active", "--quiet", "sample.service"])

    def test_systemd_failure_raises(self):
        from livingcode.guard.procman import SystemdManager

        with self.assertRaises(RuntimeError):
            SystemdManager(self.lab().cfg, self.fake(code=1)).restart()

    def test_systemd_sandbox_blocks_network(self):
        from livingcode.common.sandbox import SystemdSandbox

        command = SystemdSandbox(self.fake()).command(Path("/tmp/x"), ["python", "-V"], 30)
        for needed in ("--property=PrivateNetwork=yes", "--property=DynamicUser=yes", "--property=ProtectHome=yes"):
            self.assertIn(needed, command)

    def test_pip_installer_requires_hashes(self):
        from livingcode.common.installer import PipInstaller

        commands = PipInstaller(self.fake()).commands(Path("/tmp/x"))
        self.assertIn("--require-hashes", commands[1])
        self.assertIn(":all:", commands[1])

    def test_git_history_commands(self):
        from livingcode.guard.history import GitHistory

        lab = self.lab()
        lab.set(sync={"backend": "git"})
        history = GitHistory(lab.cfg, self.fake(output="abc123"))
        self.assertEqual(history.record("r0002", Path("/tmp/tree"), "fix"), "abc123")
        self.assertEqual(self.commands[1][3:], ["commit", "--message", "r0002: fix"])
        self.assertEqual(self.commands[2][3:], ["update-ref", "refs/heads/livingcode/live", "HEAD"])
        history.push()
        self.assertEqual(len(self.commands), 4)
        history.should_push = True
        history.push()
        self.assertEqual(self.commands[-1][3:], ["push", "origin", "refs/heads/livingcode/live"])

    def test_git_export_quotes_the_reference(self):
        from livingcode.guard.history import GitHistory

        lab = self.lab()
        target = lab.base / "export"
        GitHistory(lab.cfg, self.fake()).export("x; rm -rf /", target)
        self.assertIn("'x; rm -rf /'", self.commands[0][2])

    def test_bandit_output_is_parsed(self):
        from livingcode.guard.scan import BanditScanner

        report = json.dumps({"results": [{"filename": "./a.py", "test_id": "B602", "issue_severity": "HIGH"}]})
        lab = self.lab()
        (lab.base / "a.py").write_text("x = 1\n")
        findings = BanditScanner(self.fake(report)).scan(lab.base, ["a.py"])
        self.assertEqual((findings[0].path, findings[0].rule, findings[0].severity), ("a.py", "B602", "high"))

    def test_pip_audit_output_is_parsed(self):
        from livingcode.common.advisories import PipAuditSource

        report = json.dumps({"dependencies": [{"name": "Foo", "version": "1.0", "vulns": [
            {"id": "X-1", "fix_versions": ["1.1"]}]}]})
        found = PipAuditSource("/tmp/lock", self.fake(report)).check({})
        self.assertEqual((found[0].id, found[0].package, found[0].fixed_version), ("X-1", "foo", "1.1"))

    def test_unreadable_tool_output_is_an_error_not_a_pass(self):
        from livingcode.guard.scan import BanditScanner

        lab = self.lab()
        (lab.base / "a.py").write_text("x = 1\n")
        with self.assertRaises(RuntimeError):
            BanditScanner(self.fake("not json")).scan(lab.base, ["a.py"])


if __name__ == "__main__":
    unittest.main()
