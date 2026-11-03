"""Bring your own LLM. Only the agent may import this package, never the guard."""
from __future__ import annotations

import os
from pathlib import Path

from ..common import lab
from .anthropic import AnthropicClient
from .base import LLMClient, LLMError, LLMReply
from .fake import FakeLLM
from .openai import OpenAIClient

__all__ = ["LLMClient", "LLMError", "LLMReply", "FakeLLM", "make_client"]
KEY_NAME = "LIVINGCODE_LLM_API_KEY"


def _load_key(cfg) -> str:
    if cfg.llm.key_file:
        for line in Path(cfg.llm.key_file).read_text().splitlines():
            if line.startswith(f"{KEY_NAME}="):
                return line.split("=", 1)[1].strip()
    key = os.environ.get(KEY_NAME, "")
    if not key:
        raise LLMError(f"no LLM key found, set {KEY_NAME}")
    return key


def make_client(cfg) -> LLMClient:
    lab.check(cfg)
    if cfg.llm.provider == "fake":
        return FakeLLM.from_file(cfg.llm.fake_script)
    client = {"anthropic": AnthropicClient, "openai": OpenAIClient}[cfg.llm.provider]
    return client(_load_key(cfg), cfg.llm.model, cfg.llm.base_url)
