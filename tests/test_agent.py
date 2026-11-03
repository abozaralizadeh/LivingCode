"""Full rounds on the sample app with the local fake LLM."""
import json
import os
import time
import unittest

from tests.harness import BUG_FIX_RULES, Lab, get


def wait_for_error(lab):
    log = lab.cfg.app.log_files[0]
    for _ in range(100):
        if os.path.exists(log) and "ZeroDivisionError" in open(log).read():
            return
        time.sleep(0.02)
    raise AssertionError("the error never reached the log")


class Rounds(unittest.TestCase):
    def setUp(self):
        self.lab = Lab(settings={"capabilities": {"bug_fixing": "auto"}})
        self.addCleanup(self.lab.close)
        self.guard = self.lab.start()

    def bug(self):
        self.assertEqual(self.lab.trigger_bug(), 500)
        wait_for_error(self.lab)

    def test_error_to_fix_to_deploy(self):
        self.bug()
        agent = self.lab.agent(BUG_FIX_RULES)
        result = agent.run_round("new_error")
        self.assertEqual(result.status, "submitted", result.detail)
        folder = self.lab.cfg.candidates_dir / result.candidate_id
        self.assertEqual(self.guard.process_candidate(folder).state, "Settled")
        self.assertEqual(get(f"{self.lab.url}/share?total=10&people=0"), (200, "$0.00"))
        agent.read_reports()
        self.assertEqual(agent.store.issue(result.issue_id)["status"], "fixed")

    def test_propose_mode_waits_for_approval(self):
        self.lab.set(capabilities={"bug_fixing": "propose"})
        self.guard.cfg = self.lab.cfg
        self.bug()
        agent = self.lab.agent(BUG_FIX_RULES)
        result = agent.run_round()
        self.assertEqual(result.status, "proposed")
        self.assertEqual(self.lab.trigger_bug(), 500)
        self.assertEqual(agent.proposals()[0]["id"], result.candidate_id)
        folder = self.lab.proposal_to_candidate(result.candidate_id)
        self.assertEqual(self.guard.process_candidate(folder).state, "Settled")
        self.assertEqual(self.lab.trigger_bug(), 200)

    def test_flag_off_does_nothing(self):
        self.lab.set(enabled=False)
        self.bug()
        agent = self.lab.agent(BUG_FIX_RULES)
        self.assertEqual(agent.run_round().status, "skipped")
        self.assertEqual(agent.client.calls, [])

    def test_no_new_work_means_no_llm_call(self):
        agent = self.lab.agent(BUG_FIX_RULES)
        self.assertEqual(agent.run_round().status, "idle")
        self.assertEqual(agent.client.calls, [])

    def test_same_error_twice_is_one_issue(self):
        self.bug()
        self.lab.trigger_bug()
        self.lab.trigger_bug()
        time.sleep(0.1)
        agent = self.lab.agent()
        agent.collect_errors()
        self.assertEqual(len(agent.store.issues()), 1)

    def test_round_limit(self):
        self.lab.set(limits={"rounds_per_day": 1})
        self.bug()
        agent = self.lab.agent(BUG_FIX_RULES)
        agent.run_round()
        self.assertEqual(agent.run_round().status, "skipped")

    def test_wait_after_rollback_stops_rounds(self):
        self.lab.set(limits={"rollback_wait_hours": 24.0})
        agent = self.lab.agent()
        report = {"outcome": "RolledBack", "source": "agent", "issue_id": "", "time": time.time()}
        (self.lab.cfg.reports_dir / "00099.json").write_text(json.dumps(report))
        result = agent.run_round()
        self.assertEqual((result.status, result.detail), ("skipped", "still in the wait after a rollback"))

    def test_only_one_round_at_a_time(self):
        import fcntl

        agent = self.lab.agent()
        with open(self.lab.cfg.agent_dir / "round.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            other = self.lab.agent()
            self.assertEqual(other.run_round().detail, "a round is already running")
        self.assertEqual(agent.run_round().status, "idle")

    def test_finish_refused_until_tests_pass(self):
        self.bug()
        rules = [
            {"when": "ZeroDivisionError", "reply": {"actions": [
                {"tool": "replace", "path": "pricing.py", "old": "total / people", "new": "total / 0"},
                {"tool": "finish", "summary": "looks fine"},
            ]}},
        ]
        agent = self.lab.agent(rules)
        result = agent.run_round()
        self.assertEqual(result.status, "no_fix")
        self.assertEqual(os.listdir(self.lab.cfg.candidates_dir), [])
        last = agent.client.calls[-1]["messages"][-1]["content"]
        self.assertIn("finish refused", last)

    def test_garbage_replies_end_cleanly(self):
        self.bug()
        rules = [{"when": "", "reply": "not json at all"} for _ in range(30)]
        self.lab.set(llm={"max_turns": 4})
        result = self.lab.agent(rules).run_round()
        self.assertEqual((result.status, result.detail), ("no_fix", "out of turns"))

    def test_tries_are_limited(self):
        self.bug()
        rules = [{"when": "", "reply": {"actions": [{"tool": "run_tests"}]}} for _ in range(30)]
        self.lab.set(llm={"max_tries": 2})
        result = self.lab.agent(rules).run_round()
        self.assertEqual(result.status, "no_fix")
        self.assertIn("out of tries", result.detail)

    def test_two_failed_rounds_need_a_person(self):
        self.bug()
        agent = self.lab.agent()
        first = agent.run_round()
        self.assertEqual(first.status, "no_fix")
        self.assertEqual(agent.store.issue(first.issue_id)["status"], "open")
        agent.run_round()
        self.assertEqual(agent.store.issue(first.issue_id)["status"], "needs_person")
        self.assertIn("needs a person", (self.lab.cfg.log_dir / "alerts.log").read_text())


class ReportAndRetry(unittest.TestCase):
    def test_bad_fix_rolls_back_then_the_retry_sees_the_report(self):
        lab = Lab(settings={"capabilities": {"bug_fixing": "auto"}})
        self.addCleanup(lab.close)
        guard = lab.start()
        self.assertEqual(lab.trigger_bug(), 500)
        wait_for_error(lab)
        fixed = BUG_FIX_RULES[0]["reply"]["actions"]
        # This first try passes all the unit tests but breaks a page that only the smoke check visits.
        bad_try = [
            {"tool": "replace", "path": "routes.py", "old": 'return 200, "checkout"', "new": 'return 503, "closed"'},
            {"tool": "write_file", "path": "tests/test_zero_people.py", "content": "import unittest\n"},
        ]
        rules = [
            {"when": "ZeroDivisionError", "reply": {"actions": bad_try + [{"tool": "run_tests"}]}},
            {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "first try"}]}},
            {"when": "guard's report", "reply": {"actions": fixed}},
            {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": "second try"}]}},
        ]
        agent = lab.agent(rules)
        first = agent.run_round()
        self.assertEqual(first.status, "submitted", first.detail)
        outcome = guard.process_candidate(lab.cfg.candidates_dir / first.candidate_id)
        self.assertEqual(outcome.state, "RolledBack", outcome.detail)
        self.assertEqual(get(f"{lab.url}/checkout")[0], 200)
        second = agent.run_round()
        self.assertEqual(second.status, "submitted", second.detail)
        task = agent.client.calls[-2]["messages"][0]["content"]
        self.assertIn("Your last attempt failed", task)
        self.assertEqual(guard.process_candidate(lab.cfg.candidates_dir / second.candidate_id).state, "Settled")
        self.assertEqual(lab.trigger_bug(), 200)
        agent.read_reports()
        self.assertEqual(agent.store.issue(second.issue_id)["status"], "fixed")


if __name__ == "__main__":
    unittest.main()
