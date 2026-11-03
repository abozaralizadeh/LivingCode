"""Claude adapter. Plain HTTP so the project has no dependencies yet. So far it has only
run against a local fake server, never the real service."""
from __future__ import annotations

from .base import LLMError, LLMReply, post_json


class AnthropicClient:
    def __init__(self, key: str, model: str, base_url: str = ""):
        self.key = key
        self.model = model
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMReply:
        data = post_json(
            f"{self.base_url}/v1/messages",
            {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages},
        )
        if data.get("stop_reason") == "refusal":
            raise LLMError("the model declined this request")
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage", {})
        return LLMReply(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0), data.get("model", self.model))
