import httpx
import pytest

from agentkit import ConnectorError, RestConnector, RetryPolicy
from agentkit.connectors.rest import parse_retry_after


def make(handler, sleeps=None, **kw):
    sleeps = [] if sleeps is None else sleeps
    return RestConnector(
        "https://api.example.com",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        retry=RetryPolicy(max_attempts=3, base_delay=0.1, jitter=0),
        **kw,
    )


def test_read_list():
    conn = make(lambda r: httpx.Response(200, json=[{"id": 1}, {"id": 2}]))
    assert list(conn.read("/customers")) == [{"id": 1}, {"id": 2}]


def test_read_with_data_key_and_params():
    seen = {}

    def h(req):
        seen["q"] = dict(req.url.params)
        return httpx.Response(200, json={"data": [{"id": 7}]})

    conn = make(h, data_key="data")
    assert list(conn.read("/orders", status="open")) == [{"id": 7}]
    assert seen["q"] == {"status": "open"}


def test_auth_header_from_env(monkeypatch):
    monkeypatch.setenv("CRM_TOKEN", "s3cret")
    seen = {}

    def h(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json=[])

    list(make(h, token_env="CRM_TOKEN").read("/x"))
    assert seen["auth"] == "Bearer s3cret"


def test_missing_env_token_raises(monkeypatch):
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    with pytest.raises(ConnectorError, match="NOPE_TOKEN"):
        make(lambda r: httpx.Response(200), token_env="NOPE_TOKEN")


def test_retries_5xx_then_succeeds():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json=[{"ok": True}])

    sleeps = []
    assert list(make(h, sleeps).read("/x")) == [{"ok": True}]
    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]


def test_429_honors_retry_after():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[])

    sleeps = []
    list(make(h, sleeps).read("/x"))
    assert sleeps == [2.0]


def test_gives_up_after_max_attempts():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(500, text="boom")

    with pytest.raises(ConnectorError, match="500"):
        list(make(h).read("/x"))
    assert len(calls) == 3


def test_4xx_not_retried():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(404)

    with pytest.raises(ConnectorError, match="404"):
        list(make(h).read("/x"))
    assert len(calls) == 1


def test_network_error_retried():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("slow", request=req)
        return httpx.Response(200, json=[])

    sleeps = []
    list(make(h, sleeps).read("/x"))
    assert len(calls) == 2 and len(sleeps) == 1


def test_post_without_idempotency_key_not_retried():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(ConnectorError):
        make(h).write("/tickets", [{"a": 1}])
    assert len(calls) == 1


def test_post_with_idempotency_key_retried_and_header_sent():
    keys = []

    def h(req):
        keys.append(req.headers.get("idempotency-key"))
        return httpx.Response(503) if len(keys) == 1 else httpx.Response(201, json={})

    assert make(h).write("/tickets", [{"a": 1}], idempotency_prefix="job42") == 1
    assert keys == ["job42-0", "job42-0"]


def test_health_check():
    assert make(lambda r: httpx.Response(200, json={})).health_check() is True
    assert make(lambda r: httpx.Response(404)).health_check() is False


def test_parse_retry_after():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480.0) == 10.0
