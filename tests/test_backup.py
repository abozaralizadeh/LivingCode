import shutil
import tarfile
import unittest
from pathlib import Path

from tests.harness import Lab, get


def small_change(number):
    def mutate(tree):
        path = tree / "routes.py"
        path.write_text(path.read_text().replace("Sample shop", f"Sample shop {number}"))
    return mutate


class Backups(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.close)
        self.guard = self.lab.start()

    def test_backup_holds_the_previous_code_and_config(self):
        self.guard.deploy_ref(str(self.lab.tree(small_change(1))))
        row = self.guard.store.backups()[0]
        self.assertEqual(row["release_id"], "r0001")
        folder = Path(row["path"])
        self.assertTrue((folder / "config.toml").is_file())
        with tarfile.open(folder / "code.tar.gz") as tar:
            self.assertIn("r0001/app.py", tar.getnames())

    def test_failed_backup_stops_the_deploy(self):
        self.lab.set(safety={"backup_command": "/usr/bin/false"})
        self.guard.cfg = self.lab.cfg
        outcome = self.guard.deploy_ref(str(self.lab.tree(small_change(1))))
        self.assertEqual(outcome.state, "Stopped")
        self.assertEqual(self.guard.releases.current(), "r0001")
        self.assertEqual(get(f"{self.lab.url}/health")[0], 200)

    def test_backup_command_runs(self):
        script = self.lab.base / "dump.sh"
        script.write_text('#!/bin/sh\necho data > "$LIVINGCODE_BACKUP_DIR/data.sql"\n')
        script.chmod(0o755)
        self.lab.set(safety={"backup_command": str(script)})
        self.guard.cfg = self.lab.cfg
        self.assertEqual(self.guard.deploy_ref(str(self.lab.tree(small_change(1)))).state, "Settled")
        row = self.guard.store.backups()[0]
        self.assertTrue((Path(row["path"]) / "data.sql").is_file())
        self.assertEqual(row["data_included"], 1)

    def test_rollback_keeps_data(self):
        shared = self.lab.cfg.shared_dir / "orders.txt"
        shared.write_text("order 1\n")
        tree = self.lab.tree(lambda t: (t / "health.py").write_text("def status():\n    return 500, 'x'\n"))
        self.assertEqual(self.guard.deploy_ref(str(tree)).state, "RolledBack")
        self.assertEqual(shared.read_text(), "order 1\n")

    def test_only_the_last_five_are_kept(self):
        for number in range(7):
            self.assertEqual(self.guard.deploy_ref(str(self.lab.tree(small_change(number)))).state, "Settled")
        self.assertEqual(len(self.guard.store.backups()), 5)
        self.assertEqual(len(list(self.lab.cfg.backups_dir.iterdir())), 5)
        names = sorted(p.name for p in self.lab.cfg.releases_dir.iterdir() if not p.name.startswith("."))
        self.assertEqual(len(names), 5)
        self.assertIn(self.guard.releases.current(), names)

    def test_rollback_restores_a_missing_release_from_backup(self):
        tree = self.lab.tree(lambda t: (t / "health.py").write_text("def status():\n    return 500, 'x'\n"))

        def lose_previous(step):
            if step == "Switched":
                shutil.rmtree(self.lab.cfg.releases_dir / "r0001")

        self.guard.hook = lose_previous
        outcome = self.guard.deploy_ref(str(tree))
        self.assertEqual(outcome.state, "RolledBack", outcome.detail)
        self.assertEqual(self.guard.releases.current(), "r0001")
        self.assertEqual(get(f"{self.lab.url}/health")[0], 200)

    def test_each_release_keeps_its_own_dependencies(self):
        hashes = (self.lab.base / "index/tinyfmt/1.1.0/tinyfmt.py").read_bytes()
        import hashlib

        lock = f"tinyfmt==1.1.0 --hash=sha256:{hashlib.sha256(hashes).hexdigest()}\n"
        outcome = self.guard.deploy_ref(str(self.lab.tree(lambda t: (t / "requirements.lock").write_text(lock))))
        self.assertEqual(outcome.state, "Settled", outcome.detail)
        new = (self.guard.releases.path(self.guard.releases.current()) / "_deps/tinyfmt.py").read_text()
        old = (self.guard.releases.path("r0001") / "_deps/tinyfmt.py").read_text()
        self.assertIn("1.1.0", new)
        self.assertIn("1.0.0", old)

    def test_manual_rollback_refuses_unknown_targets(self):
        for target in ("r9999", "../../etc", ""):
            with self.subTest(target):
                with self.assertRaises(RuntimeError):
                    self.guard.manual_rollback(target or "nothing")

    def test_practice_rollback(self):
        self.assertIsNone(self.guard.practice_rollback())
        self.assertEqual(self.guard.releases.current(), "r0001")
        self.assertEqual(get(f"{self.lab.url}/health")[0], 200)
        self.assertEqual(self.guard.store.breaker()["consecutive_rollbacks"], 0)


if __name__ == "__main__":
    unittest.main()
