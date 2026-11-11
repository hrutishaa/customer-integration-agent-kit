"""The tool-calling loop, with guardrails a customer's security team will ask about.

Guardrails:

* ``read_only=True`` hides write tools from the model *and* refuses them if
  the model calls one anyway (models do call tools they were not given).
* ``confirm`` is called with every write before it runs; return False to
  block it. Default policy without ``confirm``: writes are blocked.
* ``max_tool_calls`` and ``max_steps`` bound cost and stop runaway loops.
* Bad arguments, unknown tools and tool exceptions are returned to the model
  as error messages it can recover from, never raised into your app.
* Every step lands in a :class:`Trace` for audit logs and evals.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentkit.agent.providers import Message, ModelResponse, Provider, ToolCall
from agentkit.agent.tools import Tool, ToolError, ToolRegistry

DEFAULT_SYSTEM = (
    "You are an integration assistant working inside a customer's systems. "
    "Use the tools to look up real data before answering; never invent records. "
    "Only write when the user asked for a change."
)


@dataclass
class ToolEvent:
    name: str
    arguments: Any
    status: str  # ok | error | blocked
    result: str
    ms: float


@dataclass
class Trace:
    answer: str | None = None
    stop_reason: str = ""  # final_answer | max_tool_calls | max_steps
    events: list[ToolEvent] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    steps: int = 0

    @property
    def tool_calls(self) -> list[str]:
        return [e.name for e in self.events]

    def summary(self) -> str:
        lines = [f"{e.status:>7}  {e.name}({json.dumps(e.arguments, default=str)[:80]})"
                 for e in self.events]
        lines.append(f"stop: {self.stop_reason} after {self.steps} model call(s)")
        return "\n".join(lines)


Confirm = Callable[[Tool, dict[str, Any]], bool]


class Agent:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry | list[Tool],
        *,
        system: str = DEFAULT_SYSTEM,
        read_only: bool = False,
        confirm: Confirm | None = None,
        max_tool_calls: int = 8,
        max_steps: int = 10,
        max_result_chars: int = 4000,
    ) -> None:
        if max_tool_calls < 0 or max_steps < 1:
            raise ValueError("max_tool_calls must be >= 0 and max_steps >= 1")
        self.provider = provider
        self.tools = tools if isinstance(tools, ToolRegistry) else ToolRegistry().add(*tools)
        self.system = system
        self.read_only = read_only
        self.confirm = confirm
        self.max_tool_calls = max_tool_calls
        self.max_steps = max_steps
        self.max_result_chars = max_result_chars

    def _execute(self, call: ToolCall) -> tuple[str, str]:
        if call.parse_error:
            return "error", f"{call.name}: {call.parse_error}"
        try:
            t = self.tools.get(call.name)
            args = t.validate(call.arguments)
        except ToolError as exc:
            return "error", str(exc)
        if t.writes:
            if self.read_only:
                return "blocked", f"{t.name} is a write tool and this agent is read-only"
            if self.confirm is None or not self.confirm(t, args):
                return "blocked", f"{t.name} was not approved by the operator; do not retry"
        try:
            result = t(**args)
        except Exception as exc:  # noqa: BLE001 - the model gets the error, the app keeps running
            return "error", f"{t.name} failed: {type(exc).__name__}: {exc}"
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        if len(text) > self.max_result_chars:
            text = text[: self.max_result_chars] + "...[truncated]"
        return "ok", text

    def run(self, task: str, *, history: list[Message] | None = None) -> Trace:
        """Run one task to a final answer (or until a budget runs out)."""
        trace = Trace()
        messages: list[Message] = [{"role": "system", "content": self.system}]
        messages += list(history or [])
        messages.append({"role": "user", "content": task})
        specs = self.tools.specs(include_writes=not self.read_only)
        used = 0
        budget_hit = False
        for _ in range(self.max_steps):
            # once the budget is spent the model sees no tools and must answer
            resp: ModelResponse = self.provider.complete(messages, [] if budget_hit else specs)
            trace.steps += 1
            if not resp.tool_calls:
                trace.answer = resp.content
                trace.stop_reason = "final_answer"
                break
            messages.append({
                "role": "assistant",
                "content": resp.content,
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {
                        "name": c.name,
                        "arguments": c.arguments if isinstance(c.arguments, str)
                        else json.dumps(c.arguments)}}
                    for c in resp.tool_calls
                ],
            })
            for c in resp.tool_calls:
                if used >= self.max_tool_calls:
                    budget_hit = True
                    text = "tool call budget exhausted; answer with what you have"
                    trace.events.append(ToolEvent(c.name, c.arguments, "blocked", text, 0.0))
                else:
                    used += 1
                    start = time.perf_counter()
                    status, text = self._execute(c)
                    trace.events.append(ToolEvent(c.name, c.arguments, status, text,
                                                  round((time.perf_counter() - start) * 1000, 2)))
                messages.append({"role": "tool", "tool_call_id": c.id, "content": text})
            if used >= self.max_tool_calls:
                budget_hit = True
        else:
            trace.stop_reason = "max_tool_calls" if budget_hit else "max_steps"
        trace.messages = messages
        return trace
