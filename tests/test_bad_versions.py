"""Phase 1 goal: at least 20 kinds of bad version all roll back within 2 minutes, with no AI."""
import shutil
import time
import unittest

from tests.harness import Lab, get


def prepend(name, code):
    def mutate(tree):
        path = tree / name
        path.write_text(code + "\n" + path.read_text())
    return mutate


def replace(name, old, new):
    def mutate(tree):
        path = tree / name
        text = path.read_text()
        assert old in text, old
        path.write_text(text.replace(old, new))
    return mutate


def delete(name):
    return lambda tree: (tree / name).unlink()


def write(name, content):
    return lambda tree: (tree / name).write_text(content)


COUNTED_HEALTH = """
import itertools
calls = itertools.count(1)

def status():
    number = next(calls)
    {body}
"""

BAD_VERSIONS = {
    "crash_on_import": prepend("app.py", "raise RuntimeError('boom')"),
    "syntax_error": prepend("app.py", "def broken(:"),
    "exits_with_zero": prepend("app.py", "import sys\nsys.exit(0)"),
    "exits_with_one": prepend("app.py", "import sys\nsys.exit(1)"),
    "hangs_before_listening": prepend("app.py", "import time\ntime.sleep(60)"),
    "starts_too_slowly": prepend("app.py", "import time\ntime.sleep(9)"),
    "listens_on_wrong_port": replace("app.py", 'os.environ.get("PORT", "18080")', '"0"'),
    "health_gives_500": write("health.py", "def status():\n    return 500, 'bad'\n"),
    "health_route_removed": replace("routes.py", 'if path == "/health":', 'if path == "/healthz":'),
    "health_hangs": write("health.py", "import time\n\ndef status():\n    time.sleep(30)\n    return 200, 'ok'\n"),
    "health_flips": write("health.py", COUNTED_HEALTH.format(body="return (200, 'ok') if number % 2 else (500, 'no')")),
    "health_fails_later": write("health.py", COUNTED_HEALTH.format(body="return (200, 'ok') if number < 4 else (500, 'no')")),
    "health_hangs_later": write(
        "health.py", COUNTED_HEALTH.format(body="import time\n    time.sleep(30 if number > 3 else 0)\n    return 200, 'ok'")),
    "health_kills_process": write("health.py", "import os\n\ndef status():\n    os._exit(1)\n"),
    "connection_dropped": replace("app.py", "        started = time.time()\n", "        return\n"),
    "smoke_page_gives_500": replace("routes.py", 'return 200, "checkout"', 'return 500, "checkout"'),
    "smoke_page_wrong_text": replace("routes.py", "<h1>Sample shop</h1>", "<h1>Oops</h1>"),
    "dies_soon_after_start": replace(
        "app.py", '    log.info("sample app started")\n',
        '    import threading\n    threading.Timer(0.25, lambda: os._exit(3)).start()\n    log.info("sample app started")\n',
    ),
    "ignores_stop_and_hangs": prepend(
        "app.py", "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)"),
    "entry_file_missing": delete("app.py"),
    "module_missing": delete("pricing.py"),
    "entry_file_corrupted": lambda tree: (tree / "app.py").write_bytes(b"\x00\xff\xfe garbage \x80"),
    "import_of_missing_package": prepend("routes.py", "import not_a_real_package"),
    "dependency_removed_from_lock": write("requirements.lock", ""),
}


def error_flood(tree):
    path = tree / "routes.py"
    path.write_text(
        "import logging\n" + path.read_text().replace(
            'if path == "/health":',
            'if path == "/health":\n        [logging.getLogger("sample").error("flood") for _ in range(3)]',
        )
    )


BAD_VERSIONS["error_flood"] = error_flood


class BadVersions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab(settings={"health": {"probation_minutes": 0.02}})
        cls.addClassCleanup(cls.lab.close)
        cls.guard = cls.lab.start()
        cls.live = cls.guard.releases.current()

    def test_every_bad_version_rolls_back_in_time(self):
        self.assertGreaterEqual(len(BAD_VERSIONS), 20)
        for name, mutate in BAD_VERSIONS.items():
            with self.subTest(name):
                tree = self.lab.tree(mutate)
                started = time.time()
                outcome = self.guard.deploy_ref(str(tree))
                took = time.time() - started
                shutil.rmtree(tree.parent)
                self.assertEqual(outcome.state, "RolledBack", outcome.detail)
                self.assertLess(took, 120)
                self.assertEqual(self.guard.releases.current(), self.live)
                self.assertEqual(get(f"{self.lab.url}/health")[0], 200)
                self.assertEqual(get(f"{self.lab.url}/")[0], 200)

    def test_healthy_version_settles(self):
        tree = self.lab.tree(replace("routes.py", "Sample shop", "Sample shop today"))
        outcome = self.guard.deploy_ref(str(tree))
        self.assertEqual(outcome.state, "Settled", outcome.detail)
        self.assertIn("today", get(f"{self.lab.url}/")[1])
        BadVersions.live = self.guard.releases.current()


class BuildFailures(unittest.TestCase):
    """These never reach the switch, so nothing live changes at all."""

    @classmethod
    def setUpClass(cls):
        cls.lab = Lab()
        cls.addClassCleanup(cls.lab.close)
        cls.guard = cls.lab.start()

    def test_bad_builds_stop_before_the_switch(self):
        live = self.guard.releases.current()
        bad_hash = "tinyfmt==1.0.0 --hash=sha256:" + "0" * 64 + "\n"
        unknown = "tinyfmt==9.9.9 --hash=sha256:" + "0" * 64 + "\n"
        for name, content in (("bad_hash", bad_hash), ("unknown", unknown), ("not_a_pin", "tinyfmt>=1\n")):
            with self.subTest(name):
                outcome = self.guard.deploy_ref(str(self.lab.tree(write("requirements.lock", content))))
                self.assertEqual(outcome.state, "Stopped", outcome.detail)
                self.assertEqual(self.guard.releases.current(), live)
                self.assertEqual(get(f"{self.lab.url}/health")[0], 200)


if __name__ == "__main__":
    unittest.main()
