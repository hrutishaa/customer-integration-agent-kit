"""Turn Python functions and connectors into LLM tools.

The JSON schema the model sees is generated from the function signature and
type hints, so it can never drift from the code. Arguments coming back from
the model are validated against that same schema before anything runs: a
model that invents a parameter, drops a required one or sends a string where
an int belongs gets a clear error message back instead of a stack trace.
"""
from __future__ import annotations

import inspect
import json
import re
import types
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from agentkit.connectors.base import Connector, ConnectorError, Record

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


class ToolError(ConnectorError):
    """Raised for unknown tools or arguments that do not match the schema."""


def json_schema(annotation: Any) -> dict[str, Any]:
    """Map a Python type hint to a JSON schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        return {"enum": list(args)}
    if origin in (Union, types.UnionType):
        non_null = [a for a in args if a is not type(None)]
        if len(non_null) == 1:
            return json_schema(non_null[0])
        return {"anyOf": [json_schema(a) for a in non_null]}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation in (list, tuple) or origin in (list, tuple):
        out: dict[str, Any] = {"type": "array"}
        if args and args[0] is not Ellipsis:
            out["items"] = json_schema(args[0])
        return out
    if annotation is dict or origin is dict:
        return {"type": "object"}
    raise TypeError(f"unsupported tool parameter type: {annotation!r}")


def _param_docs(doc: str) -> dict[str, str]:
    """Pull ``name: description`` lines out of a Google-style ``Args:`` block."""
    out: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped in {"Args:", "Arguments:", "Parameters:"}:
            in_args = True
            continue
        if in_args:
            if not stripped or stripped.endswith(":") and " " not in stripped:
                if stripped:
                    in_args = False
                continue
            m = re.match(r"(\w+)(?:\s*\([^)]*\))?:\s*(.+)", stripped)
            if m:
                out[m.group(1)] = m.group(2)
    return out


@dataclass
class Tool:
    """A callable the model may invoke, plus its JSON schema."""

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    writes: bool = False

    def spec(self) -> dict[str, Any]:
        """OpenAI-compatible ``tools`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate(self, args: Any) -> dict[str, Any]:
        """Check model-supplied arguments against the schema; return them."""
        if not isinstance(args, dict):
            raise ToolError(f"{self.name}: arguments must be a JSON object")
        props = self.parameters.get("properties", {})
        unknown = sorted(set(args) - set(props))
        if unknown:
            raise ToolError(f"{self.name}: unknown argument(s) {unknown}")
        missing = [p for p in self.parameters.get("required", []) if p not in args]
        if missing:
            raise ToolError(f"{self.name}: missing required argument(s) {missing}")
        for key, value in args.items():
            _check(f"{self.name}.{key}", value, props[key])
        return args

    def __call__(self, **kwargs: Any) -> Any:
        return self.fn(**kwargs)


def _check(where: str, value: Any, schema: dict[str, Any]) -> None:
    if value is None and schema.get("nullable"):
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{where}: {value!r} is not one of {schema['enum']}")
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                _check(where, value, option)
                return
            except ToolError:
                continue
        raise ToolError(f"{where}: {value!r} matches none of the allowed types")
    kind = schema.get("type")
    if kind is None:
        return
    ok = isinstance(value, _PY_TYPES[kind])
    if kind in {"integer", "number"} and isinstance(value, bool):
        ok = False  # True is an int in Python, but not in JSON schema
    if not ok:
        raise ToolError(f"{where}: expected {kind}, got {type(value).__name__}")
    if kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            _check(f"{where}[{i}]", item, schema["items"])
    if kind == "integer" or kind == "number":
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolError(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolError(f"{where}: must be <= {schema['maximum']}")


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    writes: bool = False,
) -> Any:
    """Decorator: build a :class:`Tool` from a typed, documented function.

    >>> @tool
    ... def lookup_ticket(ticket_id: int) -> dict:
    ...     '''Fetch one support ticket by id.'''
    ...     return {"id": ticket_id}
    >>> lookup_ticket.parameters["required"]
    ['ticket_id']
    """

    def build(f: Callable[..., Any]) -> Tool:
        tool_name = name or f.__name__
        if not _NAME_RE.match(tool_name):
            raise ValueError(f"invalid tool name {tool_name!r}")
        doc = inspect.getdoc(f) or ""
        summary = description or (doc.split("\n\n")[0].strip() if doc else "")
        if not summary:
            raise ValueError(f"tool {tool_name!r} needs a description or docstring")
        hints = get_type_hints(f)
        pdocs = _param_docs(doc)
        props: dict[str, Any] = {}
        required: list[str] = []
        for pname, param in inspect.signature(f).parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise TypeError(f"tool {tool_name!r}: *args/**kwargs are not allowed")
            hint = hints.get(pname, param.annotation)
            schema = json_schema(hint)
            if get_origin(hint) in (Union, types.UnionType) and type(None) in get_args(hint):
                schema["nullable"] = True
            if pname in pdocs:
                schema["description"] = pdocs[pname]
            if param.default is param.empty:
                required.append(pname)
            elif param.default is not None:
                schema["default"] = param.default
            props[pname] = schema
        params = {"type": "object", "properties": props, "required": required,
                  "additionalProperties": False}
        return Tool(tool_name, summary, params, f, writes=writes)

    return build(fn) if fn is not None else build


@dataclass
class ToolRegistry:
    """The set of tools one agent is allowed to use."""

    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, *items: Tool | Callable[..., Any]) -> ToolRegistry:
        for item in items:
            t = item if isinstance(item, Tool) else tool(item)
            if t.name in self.tools:
                raise ValueError(f"duplicate tool name {t.name!r}")
            self.tools[t.name] = t
        return self

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def __len__(self) -> int:
        return len(self.tools)

    def get(self, name: str) -> Tool:
        try:
            return self.tools[name]
        except KeyError:
            raise ToolError(f"unknown tool {name!r}; available: {sorted(self.tools)}") from None

    def specs(self, *, include_writes: bool = True) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools.values() if include_writes or not t.writes]


def _compact(rows: list[Record], limit: int, total: int) -> str:
    payload = {"rows": rows[:limit], "returned": min(len(rows), limit), "total_seen": total}
    if total > limit:
        payload["truncated"] = True
    return json.dumps(payload, default=str)


def connector_tools(
    connector: Connector,
    *,
    prefix: str | None = None,
    resources: Iterable[str] | None = None,
    max_rows: int = 50,
    writable: bool = True,
) -> list[Tool]:
    """Expose a connector as ``<prefix>_read`` and (optionally) ``<prefix>_write`` tools.

    ``resources`` becomes an ``enum`` in the schema, so the model can only
    name tables/endpoints you listed. Reads are capped at ``max_rows`` so one
    tool call cannot flood the context window.
    """
    prefix = prefix or connector.name
    allowed = list(resources) if resources is not None else None
    res_schema: dict[str, Any] = {"type": "string", "description": "Resource to use."}
    if allowed:
        res_schema["enum"] = allowed

    def read(resource: str, filters: dict | None = None, limit: int = 20) -> str:
        rows: list[Record] = []
        total = 0
        cap = max(1, min(limit, max_rows))
        for row in connector.read(resource, **(filters or {})):
            total += 1
            if len(rows) < cap:
                rows.append(row)
            if total > cap:  # we only need to know it was truncated
                break
        return _compact(rows, cap, total)

    read_tool = Tool(
        name=f"{prefix}_read",
        description=f"Read records from the {connector.name} system. Filters are exact-match "
        "field=value pairs. Returns JSON with rows and a truncated flag.",
        parameters={
            "type": "object",
            "properties": {
                "resource": res_schema,
                "filters": {"type": "object", "nullable": True,
                            "description": "Exact-match filters, e.g. {\"status\": \"open\"}."},
                "limit": {"type": "integer", "minimum": 1, "maximum": max_rows, "default": 20},
            },
            "required": ["resource"],
            "additionalProperties": False,
        },
        fn=read,
    )
    out = [read_tool]
    if writable:
        def write(resource: str, records: list, upsert_on: str | None = None) -> str:
            kwargs = {"upsert_on": upsert_on} if upsert_on else {}
            n = connector.write(resource, records, **kwargs)
            return json.dumps({"written": n})

        out.append(Tool(
            name=f"{prefix}_write",
            description=f"Create or update records in the {connector.name} system. "
            "Pass upsert_on (e.g. 'email') to update an existing match instead of duplicating.",
            parameters={
                "type": "object",
                "properties": {
                    "resource": res_schema,
                    "records": {"type": "array", "items": {"type": "object"}},
                    "upsert_on": {"type": "string", "nullable": True},
                },
                "required": ["resource", "records"],
                "additionalProperties": False,
            },
            fn=write,
            writes=True,
        ))
    return out
