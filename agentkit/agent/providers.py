"""Model providers for the tool-calling loop.

Anything with a ``complete(messages, tools)`` method works. Two ship here:

* :class:`OpenAICompatibleProvider` talks to any ``/chat/completions`` API
  (OpenAI, Azure OpenAI, Groq, Together, vLLM, Ollama, LM Studio...). The
  API key comes from an env var, is sent only as a header and is never
  logged or put in error messages.
* :class:`ScriptedProvider` replays canned responses so the loop is fully
  testable (and demo-able) without a network or a key.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from agentkit.connectors.base import ConnectorError
from agentkit.connectors.rest import RestConnector, RetryPolicy

Message = dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Any  # dict when the model sent valid JSON, else the raw string
    parse_error: str | None = None


@dataclass
class ModelResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class Provider(Protocol):
    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        ...


def parse_tool_call(raw: dict[str, Any], index: int = 0) -> ToolCall:
    fn = raw.get("function") or {}
    args = fn.get("arguments")
    call_id = raw.get("id") or f"call_{index}"
    if isinstance(args, dict):
        return ToolCall(call_id, fn.get("name", ""), args)
    try:
        parsed = json.loads(args or "{}")
    except json.JSONDecodeError as exc:
        return ToolCall(call_id, fn.get("name", ""), args, parse_error=f"invalid JSON: {exc}")
    return ToolCall(call_id, fn.get("name", ""), parsed)


class OpenAICompatibleProvider:
    """Call an OpenAI-compatible chat completions endpoint with tools."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str | None = "OPENAI_API_KEY",
        temperature: float = 0.0,
        timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        # Local servers (Ollama, vLLM) usually need no key: pass api_key_env=None.
        if api_key_env and not os.environ.get(api_key_env):
            raise ConnectorError(f"environment variable {api_key_env} is not set")
        kwargs: dict[str, Any] = {"sleep": sleep} if sleep else {}
        self._rest = RestConnector(
            base_url.rstrip("/"),
            token_env=api_key_env,
            timeout=timeout,
            retry=retry or RetryPolicy(max_attempts=4),
            transport=transport,
            **kwargs,
        )
        self.model = model
        self.temperature = temperature
        self._n = 0

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        self._n += 1
        # chat completions has no side effects, so a key makes the POST safe to retry
        resp = self._rest.request(
            "POST", "/chat/completions", json=body, idempotency_key=f"agentkit-{id(self)}-{self._n}"
        ).json()
        try:
            msg = resp["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ConnectorError(f"unexpected completion shape: {str(resp)[:200]}") from exc
        calls = [parse_tool_call(c, i) for i, c in enumerate(msg.get("tool_calls") or [])]
        return ModelResponse(msg.get("content"), calls, dict(resp.get("usage") or {}))

    def close(self) -> None:
        self._rest.close()


class ScriptedProvider:
    """Replay a fixed script of responses; records every request it saw."""

    def __init__(self, script: Iterable[ModelResponse | Callable[[list[Message]], ModelResponse]]):
        self.script = list(script)
        self.requests: list[tuple[list[Message], list[dict[str, Any]]]] = []

    def complete(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelResponse:
        self.requests.append(([dict(m) for m in messages], tools))
        if not self.script:
            return ModelResponse(content="(script finished)")
        step = self.script.pop(0)
        return step(messages) if callable(step) else step


def call(name: str, call_id: str | None = None, **arguments: Any) -> ToolCall:
    """Shorthand for scripting: ``call("crm_read", resource="contacts")``."""
    return ToolCall(call_id or f"call_{name}", name, arguments)
