"""A local stand-in for an LLM. It follows a script, costs nothing and calls nothing.

Each rule has a "when" text and a "reply". The first unused rule whose text appears in the
conversation gives the reply. This makes rounds repeatable without a paid service.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import LLMReply

GIVE_UP = {"actions": [{"tool": "give_up", "reason": "the fake LLM has no scripted reply for this"}]}


class FakeLLM:
    def __init__(self, rules: list[dict] | None = None):
        self.rules = [dict(rule) for rule in rules or []]
        self.calls: list[dict] = []

    @classmethod
    def from_file(cls, path: str) -> "FakeLLM":
        return cls(json.loads(Path(path).read_text()) if path else [])

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMReply:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        conversation = "\n".join(message["content"] for message in messages)
        reply = GIVE_UP
        for rule in self.rules:
            if rule.get("used"):
                continue
            if rule.get("when", "") in conversation:
                rule["used"] = True
                reply = rule["reply"]
                break
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMReply(text, (len(system) + len(conversation)) // 4, len(text) // 4, "fake")
