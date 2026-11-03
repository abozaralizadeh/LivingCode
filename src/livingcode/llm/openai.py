"""ChatGPT adapter. Plain HTTP so the project has no dependencies yet. So far it has only
run against a local fake server, never the real service."""
from __future__ import annotations

from .base import LLMError, LLMReply, post_json


class OpenAIClient:
    def __init__(self, key: str, model: str, base_url: str = ""):
        self.key = key
        self.model = model
        self.base_url = (base_url or "https://api.openai.com").rstrip("/")

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMReply:
        data = post_json(
            f"{self.base_url}/v1/chat/completions",
            {"Authorization": f"Bearer {self.key}"},
            {
                "model": self.model,
                "max_completion_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, *messages],
            },
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM reply had an unexpected shape") from exc
        usage = data.get("usage", {})
        return LLMReply(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), data.get("model", self.model))
