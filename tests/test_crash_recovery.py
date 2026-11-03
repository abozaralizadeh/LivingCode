"""Kills the guard for real at each step, then checks a new guard puts things right."""
import shutil
import signal
import subprocess
import sys
import unittest
from pathlib import Path

from tests.harness import Lab, get

RUNNER = str(Path(__file__).parent / "crash_runner.py")
GOOD_STEPS = ("Checking", "BackingUp", "Building", "Switching", "Switched", "Restarted", "Probation", "ProbationTick")
FAILING_STEPS = ("RollingBack", "RollbackReady", "RollbackSwitched")


def good_change(tree):
    path = tree / "routes.py"
    path.write_text(path.read_text().replace("Sample shop", "Sample shop two"))


def failing_change(tree):
    (tree / "health.py").write_text("def status():\n    return 500, 'bad'\n")


class CrashRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab()
        cls.addClassCleanup(cls.lab.close)
        cls.guard = cls.lab.start()

    def _crash_at(self, step, mutate, stop_app_too=False):
        lab = self.lab
        tree = lab.tree(mutate)
        before = self.guard.releases.current()
        done = subprocess.run([sys.executable, RUNNER, str(lab.config_path), str(tree), step], capture_output=True)
        self.assertEqual(done.returncode, -signal.SIGKILL, done.stderr.decode()[-500:])
        guard = lab.deployer()
        if stop_app_too:
            guard.pm.stop()
        outcome = guard.recover()
        self.assertIsNotNone(outcome)
        self.assertIn(outcome.state, ("Stopped", "RolledBack"), outcome.detail)
        self.assertEqual(guard.releases.current(), before)
        self.assertTrue(guard.pm.is_active())
        self.assertEqual(get(f"{lab.url}/health")[0], 200)
        self.assertNotIn("two", get(f"{lab.url}/")[1])
        self.assertIsNone(guard.store.open_deployment())
        self.assertEqual(list(guard.cfg.releases_dir.glob(".stage-*")), [])
        shutil.rmtree(tree.parent)

    def test_killed_during_a_good_deploy(self):
        for step in GOOD_STEPS:
            with self.subTest(step):
                self._crash_at(step, good_change)

    def test_killed_during_a_rollback(self):
        for step in FAILING_STEPS:
            with self.subTest(step):
                self._crash_at(step, failing_change)

    def test_machine_restart_stops_the_app_too(self):
        for step in ("Switched", "Probation", "RollbackSwitched"):
            with self.subTest(step):
                mutate = failing_change if step.startswith("Rollback") else good_change
                self._crash_at(step, mutate, stop_app_too=True)

    def test_recover_with_nothing_in_progress_starts_a_stopped_app(self):
        guard = self.lab.deployer()
        guard.pm.stop()
        self.assertIsNone(guard.recover())
        self.assertTrue(guard.pm.is_active())
        self.assertEqual(guard.store.open_deployment(), None)


if __name__ == "__main__":
    unittest.main()
