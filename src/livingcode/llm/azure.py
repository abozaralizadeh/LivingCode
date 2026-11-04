"""Azure OpenAI adapter, through the v1 chat completions API. Plain HTTP, like the others.

Only the web demo's front process uses this. The agent never holds an Azure key: it asks
the front through the relay (see relay.py), and the front makes the paid call.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import LLMError, LLMReply, post_json


@dataclass(frozen=True)
class AzureTarget:
    endpoint: str
    api_key: str
    deployment: str

    @property
    def url(self) -> str:
        base = self.endpoint.rstrip("/")
        if not base.endswith("/openai/v1"):
            base += "/openai/v1"
        return f"{base}/chat/completions"


class AzureOpenAIClient:
    def __init__(self, target: AzureTarget, timeout: float = 120):
        self.target = target
        self.timeout = timeout
        self.name = f"azure:{target.deployment}"

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMReply:
        data = post_json(
            self.target.url,
            {"api-key": self.target.api_key},
            {
                "model": self.target.deployment,
                # Reasoning models spend part of this before they answer.
                "max_completion_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, *messages],
            },
            timeout=self.timeout,
            retries=1,
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM reply had an unexpected shape") from exc
        usage = data.get("usage") or {}
        return LLMReply(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), self.target.deployment)


class FallbackClient:
    """Tries the main client, and the fallback only when the main one fails."""

    def __init__(self, main, fallback):
        self.main = main
        self.fallback = fallback
        self.name = f"{main.name}, then {fallback.name}"

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMReply:
        try:
            return self.main.complete(system, messages, max_tokens)
        except LLMError:
            return self.fallback.complete(system, messages, max_tokens)
