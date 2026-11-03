"""The small interface every LLM provider has to meet, plus a shared HTTP call."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    text: str
    input_tokens: int
    output_tokens: int
    model: str = ""


class LLMClient(Protocol):
    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMReply: ...


def post_json(url: str, headers: dict, body: dict, timeout: float = 120, retries: int = 2) -> dict:
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(retries + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429) and exc.code < 500 or attempt == retries:
                raise LLMError(f"LLM call failed with status {exc.code}") from None
            wait = float(exc.headers.get("retry-after") or 2**attempt)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == retries:
                raise LLMError(f"LLM call failed: {type(exc).__name__}") from None
            wait = 2**attempt
        time.sleep(min(wait, 30))
    raise LLMError("LLM call failed")
