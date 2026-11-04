"""The web demo: settings, limits, the lab lock's one Linux exception, the Azure adapter,
the relay, the setup script's shared VM mode, and whole runs behind a /livingcode/ prefix.

Nothing here reaches a real provider. Runs use each scenario's scripted fake LLM.
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.harness import ROOT, Lab

from livingcode.llm import FakeLLM, LLMError  # noqa: E402
from livingcode.llm.azure import AzureOpenAIClient, AzureTarget, FallbackClient  # noqa: E402
from livingcode.llm.relay import RelayClient  # noqa: E402
from livingcode.webdemo import front as front_module, settings as settings_module  # noqa: E402
from livingcode.webdemo.limits import Limits  # noqa: E402
from livingcode.webdemo.scenarios import SCENARIOS  # noqa: E402

PREFIX = "/livingcode/"


def short_tmp(test) -> Path:
    """Unix socket paths have to stay short, so these folders live right under /tmp."""
    folder = Path(tempfile.mkdtemp(prefix="lcw-", dir="/tmp")).resolve()
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return folder


def demo_settings(folder: Path, **changes) -> settings_module.Settings:
    values = {"port": 0, "trust_proxy": ("127.0.0.1",), "data_dir": folder / "data", "log_dir": folder / "logs"}
    values.update(changes)
    return settings_module.Settings(**values)


class SettingsTests(unittest.TestCase):
    def load(self, **env):
        return settings_module.load(env, env_file=Path("/nonexistent/.env"))

    def test_defaults_use_the_fake_and_stay_local(self):
        settings = self.load()
        self.assertEqual((settings.llm, settings.host, settings.port, settings.trust_proxy), ("fake", "127.0.0.1", 8606, ()))

    def test_azure_with_a_fallback(self):
        settings = self.load(
            AZURE_OPENAI_ENDPOINT="https://a.openai.azure.com", AZURE_OPENAI_API_KEY="k1", AZURE_OPENAI_DEPLOYMENT="gpt-6-luna",
            AZURE_OPENAI_FALLBACK_ENDPOINT="https://b.openai.azure.com", AZURE_OPENAI_FALLBACK_API_KEY="k2",
            AZURE_OPENAI_FALLBACK_DEPLOYMENT="gpt-5-mini", TRUST_PROXY="127.0.0.1",
        )
        self.assertEqual(settings.llm, "azure")
        self.assertEqual((settings.primary.deployment, settings.fallback.deployment), ("gpt-6-luna", "gpt-5-mini"))
        self.assertEqual(settings.trust_proxy, ("127.0.0.1",))

    def test_the_example_file_starts_on_the_fake(self):
        settings = settings_module.load({}, env_file=ROOT / ".env.example")
        self.assertEqual(settings.llm, "fake")
        self.assertEqual(settings.daily_token_budget, 400000)

    def test_half_set_azure_is_refused(self):
        with self.assertRaisesRegex(settings_module.SettingsError, "all three"):
            self.load(AZURE_OPENAI_ENDPOINT="https://a.openai.azure.com", AZURE_OPENAI_API_KEY="k")
        with self.assertRaisesRegex(settings_module.SettingsError, "https"):
            self.load(AZURE_OPENAI_ENDPOINT="http://a", AZURE_OPENAI_API_KEY="k", AZURE_OPENAI_DEPLOYMENT="d")

    def test_the_page_never_listens_beyond_this_machine(self):
        with self.assertRaisesRegex(settings_module.SettingsError, "HOST"):
            self.load(HOST="0.0.0.0")

    def test_the_environment_wins_over_the_file(self):
        folder = short_tmp(self)
        (folder / ".env").write_text("PORT=1111\nRUNS_PER_IP_PER_DAY=9\n")
        settings = settings_module.load({"PORT": "2222"}, env_file=folder / ".env")
        self.assertEqual((settings.port, settings.runs_per_ip_per_day), (2222, 9))


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.folder = short_tmp(self)
        self.day = "2026-11-04"
        self.limits = Limits(self.folder, runs_per_ip=2, run_cap=3, token_budget=1000, clock=lambda: self.day)

    def test_runs_per_visitor_and_in_all(self):
        for _ in range(2):
            self.assertIsNone(self.limits.refusal("1.1.1.1"))
            self.limits.record_run("1.1.1.1")
        self.assertIn("used all your runs", self.limits.refusal("1.1.1.1"))
        self.assertIsNone(self.limits.refusal("2.2.2.2"))
        self.limits.record_run("2.2.2.2")
        self.assertIn("all its runs for today", self.limits.refusal("3.3.3.3"))

    def test_the_token_budget(self):
        self.limits.add_tokens(999)
        self.assertEqual(self.limits.tokens_left(), 1)
        self.assertIsNone(self.limits.refusal("1.1.1.1"))
        self.limits.add_tokens(5)
        self.assertIn("AI budget", self.limits.refusal("1.1.1.1"))
        self.assertTrue(self.limits.summary("1.1.1.1")["budget_spent"])

    def test_a_new_day_starts_fresh_and_restarts_keep_the_count(self):
        self.limits.record_run("1.1.1.1")
        again = Limits(self.folder, runs_per_ip=2, run_cap=3, token_budget=1000, clock=lambda: self.day)
        self.assertEqual(again.summary("1.1.1.1")["your_runs_left"], 1)
        self.day = "2026-11-05"
        self.assertEqual(again.summary("1.1.1.1")["your_runs_left"], 2)

    def test_addresses_are_never_written_down(self):
        self.limits.record_run("198.51.100.23")
        self.assertNotIn("198.51.100.23", (self.folder / "limits.json").read_text())


class LabLockForTheRunner(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(enabled=False)
        self.addCleanup(self.lab.close)

    def problems(self):
        from livingcode.common import lab

        return lab.problems(self.lab.reload())

    def test_linux_is_refused_outside_the_sealed_runner(self):
        with mock.patch("platform.system", return_value="Linux"), mock.patch.dict(os.environ, {"LIVINGCODE_CONFINED": "1"}), \
                mock.patch("livingcode.common.lab.route_devices", return_value=["eth0", "lo"]):
            self.assertTrue(any("only allows this Mac" in item for item in self.problems()))
        with mock.patch("platform.system", return_value="Linux"), mock.patch.dict(os.environ), \
                mock.patch("livingcode.common.lab.route_devices", return_value=["lo"]):
            os.environ.pop("LIVINGCODE_CONFINED", None)
            self.assertTrue(any("only allows this Mac" in item for item in self.problems()))

    def test_the_sealed_runner_on_linux_is_allowed(self):
        with mock.patch("platform.system", return_value="Linux"), mock.patch.dict(os.environ, {"LIVINGCODE_CONFINED": "1"}), \
                mock.patch("livingcode.common.lab.route_devices", return_value=["lo"]):
            self.assertEqual(self.problems(), [])

    def test_the_relay_is_allowed_but_only_as_a_local_socket(self):
        self.lab.set(llm={"provider": "relay", "socket": "/tmp/lc-relay.sock"})
        self.assertEqual(self.problems(), [])
        self.lab.set(llm={"provider": "relay", "socket": "relay.sock"})
        self.assertTrue(any("local socket" in item for item in self.problems()))

    def test_the_confined_sandbox_refuses_anywhere_else(self):
        from livingcode.common.sandbox import ConfinedSandbox, SandboxUnavailable

        with self.assertRaises(SandboxUnavailable):
            ConfinedSandbox().run(self.lab.cfg.repo, [sys.executable, "-c", "print(1)"], 5)

    def test_linux_runner_gets_the_confined_sandbox(self):
        from livingcode.common.sandbox import ConfinedSandbox, make_sandbox

        with mock.patch("platform.system", return_value="Linux"), mock.patch.dict(os.environ, {"LIVINGCODE_CONFINED": "1"}), \
                mock.patch("livingcode.common.lab.route_devices", return_value=["lo"]):
            self.assertIsInstance(make_sandbox(self.lab.cfg), ConfinedSandbox)


class FakeAzure(BaseHTTPRequestHandler):
    seen = []
    status = 200

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAzure.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        data = json.dumps({"choices": [{"message": {"content": '{"actions": []}'}}],
                           "usage": {"prompt_tokens": 11, "completion_tokens": 4}}).encode()
        self.send_response(FakeAzure.status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class AzureAdapter(unittest.TestCase):
    """The real adapter against a local server. No Azure resource is ever contacted."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAzure)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        FakeAzure.seen = []
        FakeAzure.status = 200

    def test_v1_chat_completions_with_the_deployment_as_model(self):
        client = AzureOpenAIClient(AzureTarget(self.base + "/", "azure-key", "gpt-6-luna"))
        reply = client.complete("sys", [{"role": "user", "content": "hi"}], 3000)
        self.assertEqual((reply.input_tokens, reply.output_tokens, reply.model), (11, 4, "gpt-6-luna"))
        sent = FakeAzure.seen[0]
        self.assertEqual(sent["path"], "/openai/v1/chat/completions")
        self.assertEqual(sent["headers"]["api-key"], "azure-key")
        self.assertNotIn("authorization", sent["headers"])
        self.assertEqual(sent["body"]["model"], "gpt-6-luna")
        self.assertEqual(sent["body"]["max_completion_tokens"], 3000)
        self.assertEqual(sent["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(sent["body"]["messages"][0], {"role": "system", "content": "sys"})

    def test_an_endpoint_that_already_has_the_path(self):
        self.assertEqual(AzureTarget("https://x.openai.azure.com/openai/v1", "k", "d").url,
                         "https://x.openai.azure.com/openai/v1/chat/completions")

    def test_the_fallback_is_used_only_when_the_main_one_fails(self):
        broken = AzureOpenAIClient(AzureTarget("http://127.0.0.1:9", "k", "main"), timeout=2)
        client = FallbackClient(broken, AzureOpenAIClient(AzureTarget(self.base, "k2", "gpt-5-mini")))
        self.assertEqual(client.complete("s", [{"role": "user", "content": "x"}], 500).model, "gpt-5-mini")
        self.assertEqual(FakeAzure.seen[0]["body"]["model"], "gpt-5-mini")


class Relay(unittest.TestCase):
    def setUp(self):
        self.folder = short_tmp(self)
        self.front = front_module.Front(demo_settings(self.folder))
        self.server = front_module.start_relay(self.front)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = RelayClient(str(self.front.settings.relay_socket), timeout=10)

    def going(self, client=None):
        self.front.current = {"id": "t", "scenario": SCENARIOS["zero-people"], "started": time.time(), "tokens": 0,
                              "calls": 0, "client": client or FakeLLM([{"when": "", "reply": "{\"actions\": []}"}]), "events": 0}

    def ask(self):
        return self.client.complete("sys", [{"role": "user", "content": "hello"}], 1000)

    def test_no_call_without_a_run(self):
        with self.assertRaisesRegex(LLMError, "no run is going"):
            self.ask()

    def test_a_call_during_a_run_is_counted(self):
        self.going()
        reply = self.ask()
        self.assertEqual(reply.text, '{"actions": []}')
        self.assertEqual(self.front.current["calls"], 1)
        self.assertGreater(self.front.current["tokens"], 0)

    def test_the_daily_budget_stops_paid_calls(self):
        self.front.settings.llm = "azure"
        self.front.limits.token_budget = 100
        self.going()
        self.front.limits.add_tokens(100)
        with self.assertRaisesRegex(LLMError, "budget"):
            self.ask()

    def test_a_run_has_a_call_cap_and_a_token_cap(self):
        self.going()
        self.front.current["calls"] = front_module.MAX_CALLS_PER_RUN
        with self.assertRaisesRegex(LLMError, "all its LLM calls"):
            self.ask()
        self.going()
        self.front.current["tokens"] = self.front.settings.run_token_cap
        with self.assertRaisesRegex(LLMError, "all its AI tokens"):
            self.ask()

    def test_the_socket_is_only_for_this_user(self):
        self.assertEqual(self.front.settings.relay_socket.stat().st_mode & 0o077, 0)


class TrustedProxy(unittest.TestCase):
    def ip(self, peer, forwarded):
        handler = front_module.WebHandler.__new__(front_module.WebHandler)
        handler.client_address = (peer, 1234)
        handler.headers = {"X-Forwarded-For": forwarded}
        handler.server = SimpleNamespace(front=SimpleNamespace(settings=SimpleNamespace(trust_proxy=("127.0.0.1",))))
        return handler.client_ip()

    def test_only_the_local_proxy_is_believed(self):
        self.assertEqual(self.ip("127.0.0.1", "203.0.113.9"), "203.0.113.9")
        self.assertEqual(self.ip("198.51.100.1", "203.0.113.9"), "198.51.100.1")


class PrefixProxy(BaseHTTPRequestHandler):
    """Does what the nginx snippet does: strips /livingcode/, streams, and sets the visitor address."""

    upstream = 0

    def forward(self):
        if self.path == PREFIX.rstrip("/"):
            self.send_response(301)
            self.send_header("Location", PREFIX)
            self.end_headers()
            return
        if not self.path.startswith(PREFIX):
            self.send_response(404)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "x-forwarded-for", "x-test-visitor")}
        headers["X-Forwarded-For"] = self.headers.get("X-Test-Visitor", "203.0.113.7")
        upstream = http.client.HTTPConnection("127.0.0.1", self.upstream, timeout=90)
        upstream.request(self.command, "/" + self.path[len(PREFIX):], body, headers)
        response = upstream.getresponse()
        self.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in ("connection", "transfer-encoding", "server", "date"):
                self.send_header(name, value)
        self.end_headers()
        while True:
            chunk = response.read1(4096)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    do_GET = do_POST = forward

    def log_message(self, *args):
        pass


class Stream:
    """Reads the page's event stream through the proxy, like the browser does."""

    def __init__(self, port: int):
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
        self.connection.request("GET", PREFIX + "api/events")
        self.response = self.connection.getresponse()
        self.events = []

    def until_done(self, limit: float = 90) -> dict:
        end = time.time() + limit
        name = None
        while time.time() < end:
            line = self.response.fp.readline().decode()
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: ") and name == "run":
                event = json.loads(line[6:])
                self.events.append(event)
                if event["t"] == "done":
                    return event
        raise AssertionError("the run never ended")

    def close(self):
        self.connection.close()


class WholeRunBehindAPrefix(unittest.TestCase):
    """The page, the API and a real run, through a proxy that strips /livingcode/ like nginx."""

    def setUp(self):
        self.folder = short_tmp(self)
        self.front = front_module.Front(demo_settings(self.folder, runs_per_ip_per_day=1))
        relay = front_module.start_relay(self.front)
        web = front_module.WebServer(("127.0.0.1", 0), front_module.WebHandler)
        web.front = self.front
        proxy_handler = type("Proxy", (PrefixProxy,), {"upstream": web.server_port})
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler)
        proxy.daemon_threads = True
        for server in (relay, web, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.port = proxy.server_port

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def start(self, scenario, visitor="203.0.113.7"):
        body = json.dumps({"scenario": scenario})
        return self.call("POST", PREFIX + "api/run", body, {"Content-Type": "application/json", "X-Test-Visitor": visitor})

    def test_the_page_only_uses_relative_urls(self):
        status, headers, _ = self.call("GET", PREFIX.rstrip("/"))
        self.assertEqual((status, headers["Location"]), (301, PREFIX))
        status, headers, page = self.call("GET", PREFIX)
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        for url in re.findall(r'(?:src|href)="([^"]+)"', page.decode()):
            self.assertFalse(url.startswith("/"), f"{url} would miss the prefix")
        for name in ("app.js", "styles.css", "favicon.svg"):
            status, _, data = self.call("GET", PREFIX + "static/" + name)
            self.assertEqual(status, 200, name)
        script = (ROOT / "src/livingcode/webdemo/static/app.js").read_text()
        self.assertNotRegex(script, r"""(fetch|EventSource)\(\s*["']/""")
        self.assertEqual(self.call("GET", PREFIX + "static/../front.py")[0], 404)

    def test_bad_requests_are_turned_away(self):
        self.assertEqual(self.call("POST", PREFIX + "api/run", "scenario=zero-people",
                                   {"Content-Type": "application/x-www-form-urlencoded"})[0], 415)
        self.assertEqual(self.start("rm -rf /")[0], 400)
        self.assertEqual(self.call("POST", PREFIX + "api/run", "x" * 5000, {"Content-Type": "application/json"})[0], 413)

    def test_a_whole_run_one_at_a_time_and_the_visitor_limit(self):
        stream = Stream(self.port)
        self.addCleanup(stream.close)
        status, _, data = self.start("zero-people")
        self.assertEqual(status, 202, data)
        status, _, data = self.start("word-total", visitor="198.51.100.4")
        self.assertEqual(status, 409, data)
        done = stream.until_done()
        self.assertTrue(done["ok"], done)
        lines = [event.get("text", "") for event in stream.events]
        self.assertTrue(any("Settled" in line for line in lines))
        self.assertTrue(any("RolledBack" in line for line in lines))
        self.assertTrue(any(event["t"] == "ai" for event in stream.events))
        self.assertFalse(any(str(self.folder) in line for line in lines))
        state = json.loads(self.call("GET", PREFIX + "api/state")[2])
        self.assertFalse(state["busy"])
        self.assertEqual(state["your_runs_left"], 0)
        status, _, data = self.start("word-total")
        self.assertEqual(status, 429)
        self.assertIn("used all your runs", json.loads(data)["message"])

    def test_a_run_past_its_time_is_stopped(self):
        self.front.settings.run_timeout = 1
        stream = Stream(self.port)
        self.addCleanup(stream.close)
        self.assertEqual(self.start("unknown-coupon")[0], 202)
        done = stream.until_done(30)
        self.assertFalse(done["ok"])
        self.assertIn("took longer", done["summary"])
        for _ in range(50):
            if self.front.current is None:
                break
            time.sleep(0.1)
        self.assertIsNone(self.front.current)


class EveryScenario(unittest.TestCase):
    """Each planted bug, fixed by its scripted fake, deployed by the guard, run by the runner."""

    def run_one(self, scenario: str) -> list:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT / "src"), "HOME": tempfile.gettempdir()}
        done = subprocess.run([sys.executable, "-m", "livingcode.webdemo.runner"], input=json.dumps({"scenario": scenario}) + "\n",
                              env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
        return [json.loads(line) for line in done.stdout.splitlines()]

    def test_each_one_gets_fixed_and_the_bad_checkout_rolled_back(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario):
                events = self.run_one(scenario)
                self.assertTrue(events[-1]["ok"], events[-1])
                text = "\n".join(event.get("text", "") for event in events)
                self.assertRegex(text, rf"visitor GET {re.escape(SCENARIOS[scenario].path)} -> 500")
                self.assertIn("Settled", text)
                self.assertIn("RolledBack: smoke check", text)

    def test_an_unknown_scenario_does_nothing(self):
        self.assertEqual(self.run_one("../../etc/passwd"), [{"t": "done", "ok": False, "summary": "Unknown scenario."}])

    def test_a_lab_that_fails_its_checks_stops_before_the_ai(self):
        import io

        from livingcode.webdemo.runner import Run

        out = io.StringIO()
        run = Run(SCENARIOS["zero-people"], out)
        try:
            with mock.patch("platform.system", return_value="Linux"), \
                    mock.patch("livingcode.common.lab.route_devices", return_value=["eth0", "lo"]):
                run.run()
        finally:
            run.close()
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertFalse(events[-1]["ok"])
        self.assertIn("livingcode install failed", events[-1]["summary"])
        self.assertFalse(any(event.get("text", "").startswith("2.") for event in events))


class SetupScript(unittest.TestCase):
    def test_shared_vm_mode_writes_only_its_own_files(self):
        folder = short_tmp(self)
        env = {**os.environ, "RENDER_TO": str(folder), "SERVICE_NAME": "idea-livingcode", "PORT": "8606",
               "BASE_PATH": "/livingcode/", "NGINX_SNIPPET": "/etc/nginx/max.d/livingcode.conf"}
        subprocess.run(["bash", str(ROOT / "setup_service.sh")], env=env, check=True, capture_output=True, timeout=60)
        names = sorted(path.name for path in folder.iterdir())
        self.assertEqual(names, ["idea-livingcode-limits.conf", "idea-livingcode-runner.socket",
                                 "idea-livingcode-runner@.service", "idea-livingcode.service", "livingcode.conf"])
        front = (folder / "idea-livingcode.service").read_text()
        runner = (folder / "idea-livingcode-runner@.service").read_text()
        socket_unit = (folder / "idea-livingcode-runner.socket").read_text()
        snippet = (folder / "livingcode.conf").read_text()
        zones = (folder / "idea-livingcode-limits.conf").read_text()
        for line in ("ProtectSystem=strict", "NoNewPrivileges=yes", f"ReadWritePaths={ROOT}/data\n",
                     f"EnvironmentFile={ROOT}/.env", "LIVINGCODE_RUNNER_SOCKET=/run/idea-livingcode/runner.sock"):
            self.assertIn(line, front)
        for line in ("PrivateNetwork=yes", "ProtectHome=tmpfs", f"BindReadOnlyPaths={ROOT}", "LIVINGCODE_CONFINED=1",
                     f"-{ROOT}/.env", "RuntimeMaxSec=", "StandardInput=socket"):
            self.assertIn(line, runner)
        self.assertNotIn("EnvironmentFile", runner)
        self.assertNotIn("ReadWritePaths", runner)
        self.assertIn("MaxConnections=1", socket_unit)
        self.assertIn("location = /livingcode { return 301 /livingcode/; }", snippet)
        self.assertIn("proxy_pass http://127.0.0.1:8606/;", snippet)
        self.assertIn("proxy_buffering off;", snippet)
        self.assertIn("limit_req zone=idea_livingcode", snippet)
        self.assertIn("limit_req_zone $binary_remote_addr zone=idea_livingcode:10m", zones)
        script = (ROOT / "setup_service.sh").read_text()
        vm_part = script[script.index("web_demo_vm() {"):]
        for banned in ("apt-get", "pip install", "caddy", "ufw", "certbot", "sites-enabled", "sites-available"):
            self.assertNotIn(banned, vm_part.lower())

    def test_bad_input_is_refused(self):
        env = {**os.environ, "RENDER_TO": str(short_tmp(self)), "BASE_PATH": "livingcode", "NGINX_SNIPPET": "/etc/nginx/max.d/x.conf"}
        done = subprocess.run(["bash", str(ROOT / "setup_service.sh")], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("BASE_PATH", done.stderr)


if __name__ == "__main__":
    unittest.main()
