"""The only way the agent can reach an LLM. Everything sent is cleaned, budgeted and logged."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from ..common import alerts
from ..common.logs import append_limited
from ..llm import LLMReply
from .redact import redact

AUDIT_LIMIT = 2 * 1024 * 1024


class BudgetExceeded(RuntimeError):
    pass


def month_start() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def wrap_untrusted(label: str, text: str) -> str:
    """Marks outside text as data. It can't close the marker from the inside."""
    safe = text.replace("</untrusted_data", "<\\/untrusted_data")
    return f'<untrusted_data source="{label}">\n{safe}\n</untrusted_data>'


class SafeLLM:
    def __init__(self, cfg, client, store, round_id: int):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.round_id = round_id

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        llm = self.cfg.llm
        return (input_tokens * llm.input_usd_per_mtok + output_tokens * llm.output_usd_per_mtok) / 1_000_000

    def check_budget(self) -> None:
        llm = self.cfg.llm
        worst_reply = self._cost(0, llm.max_tokens)
        if self.store.spent_in_round(self.round_id) + worst_reply > llm.budget_per_round_usd:
            raise BudgetExceeded("round budget used up")
        if self.store.spent_since(month_start()) + worst_reply > llm.budget_per_month_usd:
            alerts.send(self.cfg, "warning", "Monthly LLM budget reached", "The agent has stopped.")
            raise BudgetExceeded("monthly budget used up")

    def ask(self, system: str, messages: list[dict]) -> LLMReply:
        self.check_budget()
        clean = [{"role": item["role"], "content": redact(item["content"])} for item in messages]
        append_limited(
            self.cfg.agent_dir / "sent-to-llm.jsonl",
            json.dumps({"time": time.time(), "round": self.round_id, "messages": clean[-1:]}),
            max_bytes=AUDIT_LIMIT,
        )
        reply = self.client.complete(redact(system), clean, self.cfg.llm.max_tokens)
        self.store.add_usage(
            self.round_id, self.cfg.llm.provider, reply.model or self.cfg.llm.model,
            reply.input_tokens, reply.output_tokens, self._cost(reply.input_tokens, reply.output_tokens),
        )
        return reply
