import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import livingcode
from livingcode import cli
from livingcode.common import config as config_module
from livingcode.common.logs import append_limited
from tests.harness import BUG_FIX_RULES, ROOT, Lab, get
from tests.test_agent import wait_for_error


class Library(unittest.TestCase):
    def setUp(self):
        self.saved = dict(livingcode._state)
        self.hook = sys.excepthook
        self.addCleanup(self.restore)

    def restore(self):
        livingcode._state.update(self.saved)
        sys.excepthook = self.hook
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, livingcode._ErrorEvents):
                root.removeHandler(handler)

    def test_off_does_nothing(self):
        lab = Lab(enabled=False)
        self.addCleanup(lab.close)
        self.assertFalse(livingcode.start(str(lab.config_path)))
        self.assertIsNone(livingcode._state["events_dir"])
        livingcode.record_error("x")
        livingcode.record_request("/", 200, 1)
        self.assertEqual(list(lab.cfg.events_dir.iterdir()) if lab.cfg.events_dir.exists() else [], [])

    def test_no_config_means_off(self):
        with mock.patch.dict(os.environ, {"LIVINGCODE_CONFIG": ""}):
            self.assertFalse(livingcode.start())
        self.assertFalse(livingcode.start("/no/such/file.toml"))

    def test_broken_config_never_raises(self):
        lab = Lab()
        self.addCleanup(lab.close)
        lab.config_path.write_text("this is not toml [[[")
        self.assertFalse(livingcode.start(str(lab.config_path)))

    def test_writing_failures_never_reach_the_app(self):
        livingcode._state.update(events_dir=Path("/no/such/place/at/all"), behaviour=True)
        livingcode.record_error("x")
        livingcode.record_request("/", 200, 1)
        livingcode.record_request(object(), "bad", "bad")

    def test_on_captures_errors(self):
        lab = Lab()
        self.addCleanup(lab.close)
        self.assertTrue(livingcode.start(str(lab.config_path)))
        lab.cfg.events_dir.mkdir(parents=True, exist_ok=True)
        try:
            raise ValueError("planted")
        except ValueError:
            logging.getLogger("app").exception("failed")
        text = (lab.cfg.events_dir / "errors.jsonl").read_text()
        self.assertIn("ValueError: planted", json.loads(text.splitlines()[0])["text"])
        from livingcode.agent import errors

        blocks, _ = errors.read_library_events(lab.cfg.events_dir, {})
        self.assertIn("ValueError", blocks[0].title)


    def test_guard_checks_arent_counted_as_visits(self):
        lab = Lab(enabled=False)
        self.addCleanup(lab.close)
        livingcode._state.update(events_dir=lab.base / "events", behaviour=True)
        livingcode.record_request("/checkout", 200, 1, "cookie", "livingcode-guard")
        self.assertFalse((lab.base / "events").exists())
        livingcode.record_request("/checkout", 200, 1, "cookie", "Mozilla/5.0")
        self.assertEqual(len(list((lab.base / "events").glob("behaviour-*.jsonl"))), 1)


class ConfigChecks(unittest.TestCase):
    """A config with a mistake is refused with a clear message, never half used."""

    def parse(self, **changes):
        from livingcode import labtools

        settings = labtools.merge(labtools.lab_settings(Path("/tmp/lc-config-test"), 18999), changes)
        return config_module.parse(labtools.render_toml(settings), Path("/tmp/lc-config-test/x.toml"))

    def test_the_lab_config_is_valid_and_off(self):
        cfg = self.parse()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.mode("bug_fixing"), "propose")

    def test_mistakes_are_refused(self):
        cases = [
            ({"capabilities": {"bug_fixing": "sometimes"}}, "capabilities.bug_fixing"),
            ({"app": {"runner": "docker"}}, "app.runner"),
            ({"app": {"runner": "systemd"}}, "app.service"),
            ({"llm": {"provider": "someone"}}, "llm.provider"),
            ({"sync": {"backend": "ftp"}}, "sync.backend"),
            ({"health": {"url": ""}}, "health.url is required"),
            ({"health": {"smoke_checks": [{"path": "/"}]}}, "needs a url"),
            ({"limits": {"deploys_per_day": "lots"}}, "should be a int"),
            ({"limits": {"deploys_per_day": True}}, "should be a int"),
            ({"limits": {"no_such_limit": 1}}, "unknown setting in [limits]"),
            ({"safety": {"new_dependency_capabilities": ["flying"]}}, "unknown capability"),
            ({"enabled": "yes"}, "enabled must be true or false"),
            ({"surprise": {"x": 1}}, "unknown setting: surprise"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(config_module.ConfigError) as caught:
                    self.parse(**changes)
                self.assertIn(message, str(caught.exception))

    def test_broken_toml_is_a_clear_error(self):
        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse("enabled = [", Path("/tmp/x.toml"))
        self.assertIn("isn't valid TOML", str(caught.exception))

    def test_turning_the_flag_on_keeps_the_rest_of_the_file(self):
        lab = Lab(enabled=False)
        self.addCleanup(lab.close)
        before = config_module.fingerprint(lab.config_path)
        config_module.set_enabled(lab.config_path, True)
        self.assertTrue(config_module.load(lab.config_path).enabled)
        self.assertEqual(config_module.fingerprint(lab.config_path), before)


class AuditLog(unittest.TestCase):
    def test_rows_can_only_be_added(self):
        import sqlite3

        from livingcode.guard.store import GuardStore

        lab = Lab(enabled=False)
        self.addCleanup(lab.close)
        store = GuardStore(lab.base / "audit-test.db")
        self.addCleanup(store.close)
        store.audit("guard", "deploy", {"release": "r0001"})
        with self.assertRaises(sqlite3.DatabaseError):
            store.conn.execute("UPDATE audit_log SET action = 'nothing'")
        with self.assertRaises(sqlite3.DatabaseError):
            store.conn.execute("DELETE FROM audit_log")
        self.assertEqual(store.audit_rows()[0]["action"], "deploy")


class SizeLimit(unittest.TestCase):
    def test_files_rotate_and_stay_limited(self):
        lab = Lab(enabled=False)
        self.addCleanup(lab.close)
        path = lab.base / "x.log"
        for number in range(500):
            append_limited(path, "y" * 50 + str(number), max_bytes=1000, files_kept=3)
        names = sorted(p.name for p in lab.base.glob("x.log*"))
        self.assertEqual(names, ["x.log", "x.log.1", "x.log.2"])
        self.assertLess(path.stat().st_size, 1100)


class Errors(unittest.TestCase):
    def test_fingerprint_ignores_changing_details(self):
        from livingcode.agent.errors import parse_blocks

        text = (
            '2026-11-02T10:00:00Z ERROR request failed: /a?x=1\nTraceback (most recent call last):\n'
            '  File "/x/pricing.py", line 5, in share\nZeroDivisionError: division by zero\n'
            '2026-11-02T10:00:01Z INFO fine\n'
            '2026-11-02T10:05:00Z ERROR request failed: /b?x=2\nTraceback (most recent call last):\n'
            '  File "/y/pricing.py", line 9, in share\nZeroDivisionError: division by zero\n'
        )
        blocks = parse_blocks(text)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].fingerprint, blocks[1].fingerprint)
        self.assertEqual(blocks[0].title, "ZeroDivisionError at pricing.py:share")


class Commands(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(enabled=False)
        self.addCleanup(self.lab.close)
        self.addCleanup(self.stop_app)

    def stop_app(self):
        with contextlib.suppress(Exception):
            self.run_cli("lab", "stop")

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main(["--config", str(self.lab.config_path), *args])
        return code, output.getvalue()

    def test_full_operator_flow(self):
        self.assertEqual(self.run_cli("install")[0], 0)
        code, text = self.run_cli("on")
        self.assertEqual(code, 1)
        self.assertIn("doctor", text)
        code, text = self.run_cli("doctor")
        self.assertEqual(code, 0, text)
        self.assertIn("practice rollback worked", text)
        self.assertIn("WARN separate guard and agent users", text)
        self.assertEqual(self.run_cli("on")[0], 0)
        self.assertTrue(config_module.load(self.lab.config_path).enabled)
        self.assertIn("Feature flag:   on", self.run_cli("status")[1])
        self.lab.trigger_bug()
        self.lab.reload()
        wait_for_error(self.lab)
        script = Path(self.lab.cfg.llm.fake_script)
        script.write_text(json.dumps(BUG_FIX_RULES))
        code, text = self.run_cli("run")
        self.assertIn("proposed", text)
        candidate = text.split("Candidate: ")[1].strip()
        self.assertIn(candidate, self.run_cli("proposals")[1])
        code, text = self.run_cli("show", candidate)
        self.assertIn("+    if people <= 0:", text)
        code, text = self.run_cli("approve", candidate)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.lab.trigger_bug(), 200)
        self.assertIn("r0002", self.run_cli("history")[1])
        self.assertIn("$", self.run_cli("cost")[1])
        code, text = self.run_cli("rollback")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.lab.trigger_bug(), 500)
        self.assertEqual(self.run_cli("off")[0], 0)
        self.assertFalse(config_module.load(self.lab.config_path).enabled)

    def test_changed_config_needs_a_new_doctor_pass(self):
        self.run_cli("install")
        self.run_cli("doctor")
        self.lab.set(limits={"rounds_per_day": 2})
        self.assertEqual(self.run_cli("on")[0], 1)

    def test_reject(self):
        self.run_cli("install")
        self.lab.set(enabled=True)
        self.lab.trigger_bug()
        wait_for_error(self.lab)
        Path(self.lab.cfg.llm.fake_script).write_text(json.dumps(BUG_FIX_RULES))
        candidate = self.run_cli("run")[1].split("Candidate: ")[1].strip()
        self.assertEqual(self.run_cli("reject", candidate)[0], 0)
        self.assertIn("No proposals", self.run_cli("proposals")[1])

    def test_paths_in_arguments_are_refused(self):
        self.run_cli("install")
        self.assertEqual(self.run_cli("approve", "../../etc")[0], 1)
        self.assertEqual(self.run_cli("reject", "../../etc")[0], 1)
        self.assertEqual(self.run_cli("show", "../../etc")[0], 1)

    def test_doctor_fails_when_the_app_is_down(self):
        self.run_cli("install")
        self.run_cli("lab", "stop")
        self.assertEqual(self.run_cli("doctor")[0], 1)
        self.assertEqual(self.run_cli("on")[0], 1)

    def test_lab_lock_shows_as_a_clear_message(self):
        (self.lab.cfg.repo / "LIVINGCODE_THROWAWAY_SAMPLE").unlink()
        code, text = self.run_cli("install")
        self.assertEqual(code, 1)
        self.assertIn("lab lock", text)


class Services(unittest.TestCase):
    def test_guard_and_agent_loops_work_as_separate_processes(self):
        lab = Lab(settings={"capabilities": {"bug_fixing": "auto"}})
        self.addCleanup(lab.close)
        lab.start()
        Path(lab.cfg.llm.fake_script).write_text(json.dumps(BUG_FIX_RULES))
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), LIVINGCODE_CONFIG=str(lab.config_path))
        guard = subprocess.Popen([sys.executable, "-m", "livingcode", "guard-service"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        agent = subprocess.Popen([sys.executable, "-c",
                                  "from livingcode.agent.service import run_forever; import os;"
                                  "run_forever(os.environ['LIVINGCODE_CONFIG'], 0.3)"],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2.5)
            lab.trigger_bug()
            deadline = time.time() + 40
            while time.time() < deadline and lab.trigger_bug() != 200:
                time.sleep(0.5)
            self.assertEqual(lab.trigger_bug(), 200)
            self.assertEqual(get(f"{lab.url}/health")[0], 200)
        finally:
            for process in (guard, agent):
                process.terminate()
                process.wait(timeout=10)


class Scripts(unittest.TestCase):
    def test_setup_script_parses(self):
        done = subprocess.run(["bash", "-n", str(ROOT / "setup_service.sh")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_alert_webhook_failure_never_blocks(self):
        lab = Lab()
        self.addCleanup(lab.close)
        lab.set(safety={"alert_webhook": "http://127.0.0.1:1/nope"})
        from livingcode.common import alerts

        started = time.time()
        alerts.send(lab.cfg, "urgent", "test")
        self.assertLess(time.time() - started, 4)

    def test_alert_webhook_receives_the_alert(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        lab = Lab()
        self.addCleanup(lab.close)
        lab.set(safety={"alert_webhook": f"http://127.0.0.1:{server.server_port}/hook"})
        from livingcode.common import alerts

        alerts.send(lab.cfg, "urgent", "Rolled back", "why")
        self.assertEqual(seen[0]["title"], "Rolled back")


if __name__ == "__main__":
    unittest.main()
