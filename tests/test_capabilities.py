"""Phases 4 to 6: updating, vulnerabilities, behaviour and new features, on the sample app."""
import json
import os
import time
import unittest

import livingcode
from tests.harness import Lab, get


class KeepingCurrent(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(settings={"capabilities": {"updating": "auto", "bug_fixing": "off"}})
        self.addCleanup(self.lab.close)
        self.guard = self.lab.start()

    def live_version(self):
        return (self.lab.cfg.current_link / "_deps/tinyfmt.py").read_text().split('"')[1]

    def test_update_waits_seven_days_and_stays_in_the_major_version(self):
        agent = self.lab.agent()
        result = agent.run_round("schedule")
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(agent.client.calls, [])
        self.assertEqual(agent.store.issue(result.issue_id)["title"], "Update tinyfmt to 1.1.0")
        folder = self.lab.cfg.candidates_dir / result.candidate_id
        self.assertEqual(self.guard.process_candidate(folder).state, "Settled")
        self.assertEqual(self.live_version(), "1.1.0")
        self.assertEqual(get(f"{self.lab.url}/share?total=10&people=2")[1], "$5.00")

    def test_vulnerability_gets_the_fixed_version(self):
        self.lab.set(capabilities={"updating": "off", "fixing_vulnerabilities": "auto"})
        self.guard.cfg = self.lab.cfg
        agent = self.lab.agent()
        result = agent.run_round("schedule")
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertIn("LAB-2026-0001", agent.store.issue(result.issue_id)["title"])
        folder = self.lab.cfg.candidates_dir / result.candidate_id
        self.assertEqual(self.guard.process_candidate(folder).state, "Settled")
        self.assertEqual(self.live_version(), "1.0.1")

    def test_vulnerable_dependency_cant_be_introduced(self):
        advisories = self.lab.base / "etc/advisories.json"
        data = json.loads(advisories.read_text())
        data.append({"id": "LAB-2", "package": "tinyfmt", "affected": ["1.1.0"], "fixed": "1.2.0"})
        advisories.write_text(json.dumps(data))
        agent = self.lab.agent()
        result = agent.run_round("schedule")
        outcome = self.guard.process_candidate(self.lab.cfg.candidates_dir / result.candidate_id)
        self.assertEqual((outcome.state, outcome.failed_check), ("Rejected", "scan"))

    def test_update_that_breaks_the_tests_goes_to_the_llm(self):
        registry = self.lab.base / "etc/registry.json"
        data = json.loads(registry.read_text())
        for item in data["tinyfmt"]:
            if item["version"] == "1.1.0":
                import hashlib

                breaking = (self.lab.base / "index/tinyfmt/2.0.0/tinyfmt.py").read_bytes()
                item["sha256"] = hashlib.sha256(breaking).hexdigest()
        registry.write_text(json.dumps(data))
        (self.lab.base / "index/tinyfmt/1.1.0/tinyfmt.py").write_bytes(breaking)
        fix = 'return tinyfmt.money(total / people).replace("EUR ", "$")'
        rules = [
            {"when": "now the tests fail", "reply": {"actions": [
                {"tool": "replace", "path": "pricing.py", "old": "return tinyfmt.money(total / people)", "new": fix},
                {"tool": "run_tests"},
            ]}},
            {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "Keep dollars after the update."}]}},
        ]
        agent = self.lab.agent(rules)
        result = agent.run_round("schedule")
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(len(agent.client.calls), 2)
        outcome = self.guard.process_candidate(self.lab.cfg.candidates_dir / result.candidate_id)
        self.assertEqual(outcome.state, "Settled", outcome.detail)

    def test_history_records_settled_releases_and_pushes(self):
        agent = self.lab.agent()
        result = agent.run_round("schedule")
        self.guard.process_candidate(self.lab.cfg.candidates_dir / result.candidate_id)
        history = self.lab.cfg.guard_dir / "history"
        self.assertTrue((history / "r0002" / "app.py").is_file())
        self.assertFalse((history / "r0002" / "_deps").exists())
        pushed = (history / "remote" / "log.jsonl").read_text()
        self.assertIn("r0002", pushed)

    def test_person_can_deploy_a_recorded_reference(self):
        agent = self.lab.agent()
        result = agent.run_round("schedule")
        self.guard.process_candidate(self.lab.cfg.candidates_dir / result.candidate_id)
        self.assertEqual(self.live_version(), "1.1.0")
        self.guard.manual_rollback()
        outcome = self.guard.deploy_ref("r0002")
        self.assertEqual(outcome.state, "Settled", outcome.detail)
        self.assertEqual(self.live_version(), "1.1.0")

    def test_human_deploy_makes_waiting_candidates_stale(self):
        agent = self.lab.agent()
        result = agent.run_round("schedule")
        tree = self.lab.tree(lambda t: (t / "routes.py").write_text((t / "routes.py").read_text()))
        self.assertEqual(self.guard.deploy_ref(str(tree)).state, "Settled")
        outcome = self.guard.process_candidate(self.lab.cfg.candidates_dir / result.candidate_id)
        self.assertEqual((outcome.state, outcome.failed_check), ("Rejected", "base"))
        agent.read_reports()
        self.assertEqual(agent.store.issue(result.issue_id)["status"], "open")


def visit(lab, route, times, cookie):
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(times):
        request = urllib.request.Request(f"{lab.url}{route}", headers={"Cookie": cookie})
        try:
            opener.open(request, timeout=2).close()
        except Exception:
            pass


class Behaviour(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(settings={"capabilities": {"bug_fixing": "off", "watching_user_behaviour": "propose"}})
        self.addCleanup(self.lab.close)
        self.guard = self.lab.start()

    def events(self):
        from livingcode.agent.behaviour import read_events

        return read_events(self.lab.cfg.events_dir)

    def test_events_hold_only_the_allowed_fields(self):
        visit(self.lab, "/share?total=10&people=2&email=a@b.example", 3, "sid=abc")
        time.sleep(0.2)
        events = self.events()
        self.assertTrue(events)
        self.assertEqual(set(events[0]), {"day", "route", "status", "ms", "session", "at"})
        raw = "".join(p.read_text() for p in self.lab.cfg.events_dir.glob("behaviour-*"))
        for text in ("a@b.example", "abc", "127.0.0.1", "total=10"):
            self.assertNotIn(text, raw)

    def test_routes_lose_ids_and_totals_hide_rare_routes(self):
        self.assertEqual(livingcode.normalize_route("/users/12345/orders?x=1"), "/users/:id/orders")
        self.assertEqual(livingcode.normalize_route("/o/8c6f1e2a-1111-2222"), "/o/:id")
        from livingcode.agent.behaviour import totals

        events = [{"route": "/a", "status": 200, "ms": 10, "session": str(i), "at": i} for i in range(6)]
        events.append({"route": "/rare", "status": 200, "ms": 10, "session": "x", "at": 9})
        result = totals(events, 5)
        self.assertEqual([r["route"] for r in result["routes"]], ["/a"])

    def test_only_totals_reach_the_llm_and_the_change_waits_for_a_person(self):
        visit(self.lab, "/", 6, "sid=abc")
        visit(self.lab, "/checkout", 6, "sid=def")
        time.sleep(0.2)
        rules = [
            {"when": "behaviour-totals", "reply": {"actions": [
                {"tool": "replace", "path": "routes.py", "old": '"<h1>Sample shop</h1>"',
                 "new": '"<h1>Sample shop</h1><a href=\\"/checkout\\">Buy</a>"'},
                {"tool": "run_tests"},
            ]}},
            {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "Link to checkout."}]}},
        ]
        agent = self.lab.agent(rules)
        result = agent.run_round("schedule")
        self.assertEqual(result.status, "proposed", result.detail)
        sent = json.dumps(agent.client.calls)
        self.assertIn('\\"visits\\": 6', sent)
        self.assertIn("/checkout", sent)
        self.assertNotIn("session", json.loads(agent.store.issue(result.issue_id)["detail"])["routes"][0])
        self.assertEqual(self.guard.process_candidate(self._as_unapproved(result)).failed_check, "approval")

    def _as_unapproved(self, result):
        source = self.lab.cfg.proposals_dir / result.candidate_id
        target = self.lab.cfg.candidates_dir / result.candidate_id
        os.replace(source, target)
        return target

    def test_auto_mode_still_needs_approval_for_behaviour(self):
        self.lab.set(capabilities={"watching_user_behaviour": "auto"})
        self.guard.cfg = self.lab.cfg
        folder = self.lab.candidate("b1", [{"path": "extra.py", "content": "x = 1\n"}],
                                    capability="watching_user_behaviour")
        self.assertEqual(self.guard.process_candidate(folder).failed_check, "approval")

    def test_old_raw_events_are_deleted(self):
        from livingcode.agent.behaviour import purge_old

        old = self.lab.cfg.events_dir / "behaviour-20200101.jsonl"
        old.write_text("{}\n")
        os.utime(old, (0, 0))
        self.assertEqual(purge_old(self.lab.cfg.events_dir, 7), 1)
        self.assertFalse(old.exists())

    def test_off_means_nothing_is_recorded(self):
        self.lab.set(capabilities={"watching_user_behaviour": "off"})
        self.guard.cfg = self.lab.cfg
        self.guard.pm.restart()
        time.sleep(1.5)
        visit(self.lab, "/", 3, "x")
        time.sleep(0.2)
        before = len(self.events())
        visit(self.lab, "/", 3, "x")
        time.sleep(0.2)
        self.assertEqual(len(self.events()), before)


FEATURE_FILE = (
    "def page():\n    return 200, 'Plans: small, medium, large'\n"
)
FEATURE_RULES = [
    {"when": "Show a prices page", "reply": {"actions": [
        {"tool": "write_file", "path": "prices.py", "content": FEATURE_FILE},
        {"tool": "replace", "path": "routes.py", "old": "import pricing\n", "new": "import pricing\nimport prices\n"},
        {"tool": "replace", "path": "routes.py", "old": '    return 404, "not found"',
         "new": '    if path == "/prices":\n        return prices.page()\n    return 404, "not found"'},
        {"tool": "write_file", "path": "tests/test_prices.py",
         "content": "import unittest\nimport prices\n\n\nclass P(unittest.TestCase):\n    def test_it(self):\n"
                    "        self.assertEqual(prices.page()[0], 200)\n"},
        {"tool": "run_tests"},
    ]}},
    {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "Add a prices page."}]}},
]


class Features(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(settings={"capabilities": {"bug_fixing": "off", "adding_features": "auto"}})
        self.addCleanup(self.lab.close)
        self.guard = self.lab.start()

    def test_operator_feature_needs_approval_until_there_is_a_track_record(self):
        agent = self.lab.agent(FEATURE_RULES)
        result = agent.run_round("schedule")
        self.assertEqual(result.status, "submitted", result.detail)
        folder = self.lab.cfg.candidates_dir / result.candidate_id
        self.lab.approve(folder)
        digest_file = self.lab.cfg.approvals_dir / f"{folder.name}.json"
        digest_file.unlink()
        outcome = self.guard.process_candidate(folder)
        self.assertEqual(outcome.failed_check, "approval")

    def test_approved_feature_goes_live(self):
        agent = self.lab.agent(FEATURE_RULES)
        result = agent.run_round("schedule")
        folder = self.lab.cfg.candidates_dir / result.candidate_id
        self.lab.approve(folder)
        self.assertEqual(self.guard.process_candidate(folder).state, "Settled")
        self.assertIn("Plans", get(f"{self.lab.url}/prices")[1])

    def test_auto_feature_after_a_track_record_needs_no_approval(self):
        self.lab.set(limits={"track_record_releases": 0})
        self.guard.cfg = self.lab.cfg
        agent = self.lab.agent(FEATURE_RULES)
        result = agent.run_round("schedule")
        folder = self.lab.cfg.candidates_dir / result.candidate_id
        self.assertEqual(self.guard.process_candidate(folder).state, "Settled")

    def test_agents_own_suggestion_always_waits(self):
        self.lab.set(limits={"track_record_releases": 0})
        self.guard.cfg = self.lab.cfg
        (self.lab.base / "etc/features.toml").write_text("")
        rules = [
            {"when": "Suggest one small", "reply": {"actions": [
                {"tool": "write_file", "path": "extra.py", "content": "x = 1\n"}, {"tool": "run_tests"}]}},
            {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "Add a small helper."}]}},
        ]
        agent = self.lab.agent(rules)
        result = agent.run_round("schedule")
        self.assertEqual(result.status, "proposed", result.detail)
        forged = self.lab.candidate("forged", [{"path": "extra.py", "content": "x = 1\n"}],
                                    capability="adding_features", feature_id="made-up")
        self.assertEqual(self.guard.process_candidate(forged).failed_check, "approval")

    def test_feature_size_limit(self):
        self.lab.set(limits={"feature_max_changed_files": 2, "track_record_releases": 0})
        self.guard.cfg = self.lab.cfg
        changes = [{"path": f"f{i}.py", "content": "x = 1\n"} for i in range(3)]
        folder = self.lab.candidate("many", changes, capability="adding_features", feature_id="price-page")
        self.assertEqual(self.guard.process_candidate(folder).failed_check, "size")


if __name__ == "__main__":
    unittest.main()
