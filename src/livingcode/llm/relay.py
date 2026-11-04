"""The agent's side of the web demo's LLM relay: plain HTTP over a local unix socket.

In the web demo the agent runs with no network and no key. It sends each request to the
front process through this socket, and the front decides whether the call is allowed.
"""
from __future__ import annotations

import http.client
import json
import socket

from .base import LLMError, LLMReply


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("relay", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.path)
        self.sock = sock


class RelayClient:
    def __init__(self, path: str, timeout: float = 240):
        self.path = path
        self.timeout = timeout

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> LLMReply:
        body = json.dumps({"system": system, "messages": messages, "max_tokens": max_tokens})
        connection = _UnixConnection(self.path, self.timeout)
        try:
            connection.request("POST", "/complete", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = json.loads(response.read() or b"{}")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise LLMError(f"the LLM relay can't be reached: {type(exc).__name__}") from None
        finally:
            connection.close()
        if response.status != 200:
            raise LLMError(str(data.get("error") or f"the LLM relay said {response.status}")[:300])
        return LLMReply(str(data.get("text", "")), int(data.get("input_tokens", 0)),
                        int(data.get("output_tokens", 0)), str(data.get("model", "")))
