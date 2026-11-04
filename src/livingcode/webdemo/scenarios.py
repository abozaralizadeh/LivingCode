"""The bugs a visitor can pick from. Each one is fixed text in this file, so a visitor chooses
a scenario by its id and never sends code or a prompt of their own.

The first two bugs are already in the sample app. The third one is planted into the lab's
copy before the first release, so the sample app itself stays the same.

Each scenario also has a script for the local fake LLM, used when no Azure key is set and
by the tests. With a real model the fix is whatever it writes, and it may not find one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ZERO_FIX = "if people <= 0:\n        return tinyfmt.money(0)\n    return tinyfmt.money(total / people)"
ZERO_TEST = (
    "import unittest\nimport pricing\n\n\nclass ZeroPeople(unittest.TestCase):\n"
    "    def test_zero(self):\n        self.assertEqual(pricing.share(10, 0), '$0.00')\n"
)
TOTAL_OLD = '        total = float(query.get("total", "0"))\n'
TOTAL_FIX = (
    '        try:\n            total = float(query.get("total", "0"))\n'
    '        except ValueError:\n            return 400, "total must be a number"\n'
)
TOTAL_TEST = (
    "import unittest\nimport routes\n\n\nclass BadTotal(unittest.TestCase):\n"
    "    def test_word_total(self):\n"
    "        self.assertEqual(routes.handle('/share', {'total': 'ten', 'people': '2'})[0], 400)\n"
)
COUPONS = '''import tinyfmt

CODES = {"SAVE10": 10, "HALF": 50}


def apply(total, code):
    # Planted bug for the web demo: an unknown code crashes with KeyError.
    return tinyfmt.money(total * (100 - CODES[code]) / 100)
'''
COUPON_ROUTE = '''    if path == "/coupon":
        return 200, coupons.apply(float(query.get("total", "0")), query.get("code", ""))
'''
COUPON_TEST = (
    "import unittest\nimport coupons\n\n\nclass UnknownCode(unittest.TestCase):\n"
    "    def test_unknown_code_gives_no_discount(self):\n"
    "        self.assertEqual(coupons.apply(50, 'FREE'), '$50.00')\n"
)


def _finish(summary: str) -> dict:
    return {"when": "tests passed", "reply": {"actions": [{"tool": "finish", "summary": summary}]}}


def _plant_coupons(repo: Path) -> None:
    (repo / "coupons.py").write_text(COUPONS)
    routes = repo / "routes.py"
    text = routes.read_text()
    text = text.replace("import health\n", "import coupons\nimport health\n", 1)
    text = text.replace('    if path == "/checkout":\n', COUPON_ROUTE + '    if path == "/checkout":\n', 1)
    routes.write_text(text)


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    story: str
    path: str
    fake_rules: list = field(default_factory=list)
    plant: object = None

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "story": self.story, "path": self.path}


SCENARIOS = {
    item.id: item
    for item in (
        Scenario(
            "zero-people",
            "Split a bill with nobody",
            "Someone splits a 10 dollar bill between 0 people, and the shop crashes.",
            "/share?total=10&people=0",
            [
                {"when": "ZeroDivisionError", "reply": {"thought": "share() divides by people, guard zero.", "actions": [
                    {"tool": "read_file", "path": "pricing.py"},
                    {"tool": "replace", "path": "pricing.py", "old": "return tinyfmt.money(total / people)", "new": ZERO_FIX},
                    {"tool": "write_file", "path": "tests/test_zero_people.py", "content": ZERO_TEST},
                    {"tool": "run_tests"},
                ]}},
                _finish("Handle zero people in share."),
            ],
        ),
        Scenario(
            "word-total",
            "Type a word for a price",
            "Someone types \"ten\" where the shop wants a number, and the page falls over.",
            "/share?total=ten&people=2",
            [
                {"when": "could not convert string to float", "reply": {"thought": "Bad input should be a 400, not a crash.", "actions": [
                    {"tool": "read_file", "path": "routes.py"},
                    {"tool": "replace", "path": "routes.py", "old": TOTAL_OLD, "new": TOTAL_FIX},
                    {"tool": "write_file", "path": "tests/test_bad_total.py", "content": TOTAL_TEST},
                    {"tool": "run_tests"},
                ]}},
                _finish("Answer 400 when the total isn't a number."),
            ],
        ),
        Scenario(
            "unknown-coupon",
            "Use a coupon that doesn't exist",
            "Someone tries the code FREE at checkout. Only SAVE10 and HALF exist, so it crashes.",
            "/coupon?total=50&code=FREE",
            [
                {"when": "KeyError", "reply": {"thought": "Unknown codes should just give no discount.", "actions": [
                    {"tool": "read_file", "path": "coupons.py"},
                    {"tool": "replace", "path": "coupons.py", "old": "CODES[code]", "new": "CODES.get(code, 0)"},
                    {"tool": "write_file", "path": "tests/test_unknown_coupon.py", "content": COUPON_TEST},
                    {"tool": "run_tests"},
                ]}},
                _finish("Give no discount for an unknown coupon code."),
            ],
            plant=_plant_coupons,
        ),
    )
}
