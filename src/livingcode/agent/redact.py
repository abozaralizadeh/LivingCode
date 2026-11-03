"""Fixed rules that remove secrets and personal details before anything reaches an LLM."""
from __future__ import annotations

import re

RULES = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("jwt", re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")),
    ("api-key", re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("url-login", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    ("secret-value", re.compile(
        r"""(?i)(?<![\w-])(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|session[_-]?id)"""
        r"""(["']?\s*[:=]\s*["']?)[^\s"'&,;]+""")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("ip", re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}(?![\w:])")),
)
CARD = re.compile(r"(?<![\d.])\d(?:[ -]?\d){12,18}(?![\d.])")
PHONE = re.compile(r"(?<![\w.])\+\d[\d ()-]{7,}\d(?!\w)")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group())
    return "[REDACTED:card]" if _luhn(digits) else match.group()


def redact(text: str) -> str:
    for name, pattern in RULES:
        if name == "secret-value":
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:secret]", text)
        else:
            text = pattern.sub(f"[REDACTED:{name}]", text)
    text = CARD.sub(_card, text)
    return PHONE.sub("[REDACTED:phone]", text)
