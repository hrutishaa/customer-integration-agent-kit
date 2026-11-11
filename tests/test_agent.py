import json
from typing import Literal

import httpx
import pytest

from agentkit import ConnectorError, CRMConnector, CSVConnector, RetryPolicy
from agentkit.agent import (
    Agent,
    ModelResponse,
    OpenAICompatibleProvider,
    ScriptedProvider,
    ToolError,
    ToolRegistry,
    call,
    connector_tools,
    json_schema,
    tool,
)
from agentkit.mock import AcmeCRM

TOKEN = "acme-test-token"


# ----- schema generation ------------------------------------------------------
@tool
def find_tickets(status: Literal["open", "closed"], limit: int = 10, tag: str | None = None):
    """Find support tickets.

    Args:
        status: Ticket status to match.
        limit: Max tickets to return.
    """
    return [{"id": 1, "status": status}][:limit]


def test_schema_comes_from_signature_and_docstring():
    p = find_tickets.parameters
    assert find_tickets.description == "Find support tickets."
    assert p["required"] == ["status"]
    assert p["properties"]["status"] == {"enum": ["open", "closed"],
                                         "description": "Ticket status to match."}
    assert p["properties"]["limit"]["type"] == "integer"
    assert p["properties"]["limit"]["default"] == 10
    assert p["properties"]["tag"] == {"type": "string", "nullable": True}
    assert p["additionalProperties"] is False
    assert find_tickets.spec()["function"]["name"] == "find_tickets"


def test_json_schema_types():
    assert json_schema(list[int]) == {"type": "array", "items": {"type": "integer"}}
    assert json_schema(dict[str, int]) == {"type": "object"}
    assert json_schema(float) == {"type": "number"}
    assert json_schema(int | str) == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    with pytest.raises(TypeError):
        json_schema(set)


def test_tool_needs_description_and_no_kwargs():
    with pytest.raises(ValueError):
        tool(lambda x: x, name="nodoc")

    def bad(**kw):
        """Bad."""

    with pytest.raises(TypeError):
        tool(bad)


@pytest.mark.parametrize("args,msg", [
    ({}, "missing required"),
    ({"status": "open", "sql": "DROP TABLE"}, "unknown argument"),
    ({"status": "pending"}, "not one of"),
    ({"status": "open", "limit": "5"}, "expected integer"),
    ({"status": "open", "limit": True}, "expected integer"),
    ("status=open", "JSON object"),
])
def test_bad_model_arguments_are_rejected(args, msg):
    with pytest.raises(ToolError, match=msg):
        find_tickets.validate(args)


def test_registry_rejects_duplicates_and_unknown():
    reg = ToolRegistry().add(find_tickets)
    with pytest.raises(ValueError):
        reg.add(find_tickets)
    with pytest.raises(ToolError, match="unknown tool"):
        reg.get("delete_everything")


# ----- agent loop -------------------------------------------------------------
def answer(text):
    return ModelResponse(content=text)


def calls(*cs):
    return ModelResponse(tool_calls=list(cs))


def test_loop_runs_tool_and_feeds_result_back():
    provider = ScriptedProvider([calls(call("find_tickets", "c1", status="open")),
                                 answer("1 open ticket")])
    trace = Agent(provider, [find_tickets]).run("how many open tickets?")
    assert trace.answer == "1 open ticket"
    assert trace.stop_reason == "final_answer"
    assert trace.tool_calls == ["find_tickets"]
    tool_msg = provider.requests[1][0][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    assert json.loads(tool_msg["content"]) == [{"id": 1, "status": "open"}]


def test_errors_go_back_to_model_not_to_app():
    @tool
    def flaky() -> str:
        """Always fails."""
        raise RuntimeError("upstream 503")

    provider = ScriptedProvider([
        calls(call("nope"), call("find_tickets", status="maybe"), call("flaky")),
        answer("sorry, the ticket system is down"),
    ])
    trace = Agent(provider, [find_tickets, flaky]).run("x")
    assert [e.status for e in trace.events] == ["error", "error", "error"]
    assert "unknown tool" in trace.events[0].result
    assert "upstream 503" in trace.events[2].result
    assert trace.answer.startswith("sorry")


@tool(writes=True)
def refund(order_id: int, amount: float) -> dict:
    """Issue a refund."""
    refunds.append((order_id, amount))
    return {"refunded": amount}


refunds: list = []


def test_writes_blocked_without_confirmation():
    refunds.clear()
    provider = ScriptedProvider([calls(call("refund", order_id=7, amount=20.0)), answer("ok")])
    trace = Agent(provider, [refund]).run("refund order 7")
    assert trace.events[0].status == "blocked" and refunds == []


def test_confirm_callback_sees_args_and_can_approve():
    refunds.clear()
    seen = []

    def confirm(t, args):
        seen.append((t.name, args))
        return args["amount"] <= 50

    provider = ScriptedProvider([
        calls(call("refund", order_id=7, amount=20.0), call("refund", order_id=8, amount=900.0)),
        answer("done"),
    ])
    trace = Agent(provider, [refund], confirm=confirm).run("refunds")
    assert [e.status for e in trace.events] == ["ok", "blocked"]
    assert refunds == [(7, 20.0)]
    assert seen[1] == ("refund", {"order_id": 8, "amount": 900.0})


def test_read_only_hides_and_refuses_write_tools():
    refunds.clear()
    provider = ScriptedProvider([calls(call("refund", order_id=1, amount=1.0)), answer("no")])
    trace = Agent(provider, [find_tickets, refund], read_only=True,
                  confirm=lambda *_: True).run("x")
    offered = [t["function"]["name"] for t in provider.requests[0][1]]
    assert offered == ["find_tickets"]
    assert trace.events[0].status == "blocked" and refunds == []


def test_max_tool_calls_stops_runaway_loop():
    loop_forever = [calls(call("find_tickets", status="open")) for _ in range(20)]
    provider = ScriptedProvider(loop_forever)
    trace = Agent(provider, [find_tickets], max_tool_calls=3, max_steps=5).run("x")
    ok = [e for e in trace.events if e.status == "ok"]
    assert len(ok) == 3
    assert trace.stop_reason == "max_tool_calls"
    assert provider.requests[-1][1] == []  # model loses tool access once over budget


def test_max_steps():
    provider = ScriptedProvider([calls(call("find_tickets", status="open"))] * 5)
    trace = Agent(provider, [find_tickets], max_tool_calls=100, max_steps=2).run("x")
    assert trace.stop_reason == "max_steps" and trace.steps == 2


def test_long_results_are_truncated():
    @tool
    def dump() -> str:
        """Big."""
        return "x" * 10_000

    provider = ScriptedProvider([calls(call("dump")), answer("ok")])
    trace = Agent(provider, [dump], max_result_chars=100).run("x")
    assert trace.events[0].result.endswith("[truncated]")
    assert len(trace.events[0].result) < 120


# ----- connectors as tools ----------------------------------------------------
@pytest.fixture
def acme(monkeypatch):
    crm = AcmeCRM(token=TOKEN)
    crm.seed("contacts", [
        {"email": f"u{i}@acme.com", "name": f"User {i}", "stage": "lead" if i % 2 else "won"}
        for i in range(30)
    ])
    monkeypatch.setenv("ACME_TOKEN", TOKEN)
    conn = CRMConnector("https://acme.test", token_env="ACME_TOKEN", transport=crm.transport,
                        retry=RetryPolicy(max_attempts=2, base_delay=0, jitter=0),
                        sleep=lambda s: None)
    return crm, conn


def test_connector_tools_schema_and_capped_reads(acme):
    _, conn = acme
    read, write = connector_tools(conn, resources=["contacts"], max_rows=5)
    assert read.name == "crm_read" and write.name == "crm_write" and write.writes
    assert read.parameters["properties"]["resource"]["enum"] == ["contacts"]
    with pytest.raises(ToolError, match="not one of"):
        read.validate({"resource": "users"})
    with pytest.raises(ToolError, match="<= 5"):
        read.validate({"resource": "contacts", "limit": 500})
    out = json.loads(read(resource="contacts", limit=5))
    assert out["returned"] == 5 and out["truncated"] is True


def test_agent_upserts_into_mock_crm_with_approval(acme):
    crm, conn = acme
    reg = ToolRegistry().add(*connector_tools(conn, resources=["contacts"]))
    provider = ScriptedProvider([
        calls(call("crm_read", resource="contacts", limit=3)),
        calls(call("crm_write", resource="contacts", upsert_on="email", records=[
            {"email": "u1@acme.com", "stage": "won"},
            {"email": "new@acme.com", "name": "New Person"},
        ])),
        answer("Moved u1 to won and added new@acme.com"),
    ])
    trace = Agent(provider, reg, confirm=lambda *_: True).run("update the CRM")
    assert [e.status for e in trace.events] == ["ok", "ok"]
    rows = {r["email"]: r for r in crm.all("contacts")}
    assert rows["u1@acme.com"]["stage"] == "won"
    assert "new@acme.com" in rows and len(rows) == 31

    # same plan again: idempotent, no duplicates
    provider.script = [calls(call("crm_write", resource="contacts", upsert_on="email",
                                  records=[{"email": "new@acme.com", "name": "New Person"}])),
                       answer("done")]
    Agent(provider, reg, confirm=lambda *_: True).run("again")
    assert len(crm.all("contacts")) == 31


def test_csv_connector_read_only_tool(tmp_path):
    (tmp_path / "tickets.csv").write_text("id,status\n1,open\n2,closed\n3,open\n")
    tools = connector_tools(CSVConnector(tmp_path), prefix="csv", writable=False)
    assert [t.name for t in tools] == ["csv_read"]
    out = json.loads(tools[0](resource="tickets", filters={"status": "open"}))
    assert [r["id"] for r in out["rows"]] == ["1", "3"]


# ----- OpenAI-compatible provider --------------------------------------------
def completion(message, usage=None):
    return {"choices": [{"message": message}], "usage": usage or {"total_tokens": 42}}


def test_openai_provider_request_and_parsing(monkeypatch):
    monkeypatch.setenv("LLM_KEY", "sk-secret-123")
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=completion({
            "content": None,
            "tool_calls": [
                {"id": "a", "function": {"name": "find_tickets",
                                         "arguments": '{"status": "open"}'}},
                {"id": "b", "function": {"name": "find_tickets", "arguments": "{oops"}},
            ],
        }))

    p = OpenAICompatibleProvider("gpt-test", base_url="https://llm.test/v1",
                                 api_key_env="LLM_KEY", transport=httpx.MockTransport(handler))
    resp = p.complete([{"role": "user", "content": "hi"}], [find_tickets.spec()])
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer sk-secret-123"
    assert body["model"] == "gpt-test" and body["tools"][0]["function"]["name"] == "find_tickets"
    assert resp.tool_calls[0].arguments == {"status": "open"}
    assert resp.tool_calls[1].parse_error and resp.usage == {"total_tokens": 42}

    # a malformed-JSON call becomes an error the model can fix, not a crash
    status, text = Agent(p, [find_tickets])._execute(resp.tool_calls[1])
    assert status == "error" and "invalid JSON" in text


def test_openai_provider_retries_429_and_hides_key(monkeypatch):
    monkeypatch.setenv("LLM_KEY", "sk-secret-123")
    responses = iter([httpx.Response(429, headers={"Retry-After": "0"}),
                      httpx.Response(200, json=completion({"content": "hello"})),
                      httpx.Response(401, json={"error": "bad key"})])
    p = OpenAICompatibleProvider("m", base_url="https://llm.test/v1", api_key_env="LLM_KEY",
                                 transport=httpx.MockTransport(lambda r: next(responses)),
                                 sleep=lambda s: None,
                                 retry=RetryPolicy(max_attempts=3, base_delay=0, jitter=0))
    assert p.complete([], []).content == "hello"
    with pytest.raises(ConnectorError) as err:
        p.complete([], [])
    assert "sk-secret" not in str(err.value)


def test_openai_provider_needs_key_unless_local(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConnectorError, match="OPENAI_API_KEY"):
        OpenAICompatibleProvider("gpt-4o-mini")
    local = OpenAICompatibleProvider("llama3.1", base_url="http://localhost:11434/v1",
                                     api_key_env=None)
    local.close()
