import json

import pytest

from agentkit import WebhookError, WebhookReceiver, sign

SECRET = "whsec_test"
NOW = 1_800_000_000


def make(**kw):
    return WebhookReceiver([SECRET], clock=lambda: NOW, **kw)


def signed(body: bytes, secret=SECRET, ts=NOW):
    return {"X-Signature": sign(secret, body, ts)}


BODY = b'{"id":"evt_1","type":"ticket.created","data":{"name":"Jos\\u00e9","amount":10.50}}'


def test_valid_event_dispatches_to_handler():
    rx = make()
    rx.on("ticket.created", lambda e: e["data"]["name"])
    res = rx.handle(BODY, signed(BODY))
    assert (res.status, res.event_id, res.duplicate, res.result) == (200, "evt_1", False, "José")


def test_signature_is_over_raw_bytes_not_reserialized_json():
    # re-serialising parsed JSON changes the bytes (unicode escape, 10.50 -> 10.5)
    reserialized = json.dumps(json.loads(BODY)).encode()
    assert reserialized != BODY
    with pytest.raises(WebhookError):
        make().verify(reserialized, signed(BODY))


def test_verify_refuses_parsed_json():
    with pytest.raises(TypeError):
        make().verify(json.loads(BODY), signed(BODY))


def test_tampered_body_rejected():
    with pytest.raises(WebhookError) as e:
        make().handle(BODY.replace(b"10.50", b"99.99"), signed(BODY))
    assert e.value.status == 401


def test_wrong_secret_rejected():
    with pytest.raises(WebhookError, match="invalid signature"):
        make().handle(BODY, signed(BODY, secret="attacker"))


@pytest.mark.parametrize("header", [{}, {"X-Signature": "garbage"}, {"X-Signature": "t=abc,v1=00"}])
def test_missing_or_malformed_header(header):
    with pytest.raises(WebhookError) as e:
        make().handle(BODY, header)
    assert e.value.status == 401


def test_header_name_is_case_insensitive():
    assert make().handle(BODY, {"x-signature": sign(SECRET, BODY, NOW)}).status == 200


@pytest.mark.parametrize("ts", [NOW - 301, NOW + 301])
def test_stale_or_future_timestamp_rejected(ts):
    with pytest.raises(WebhookError, match="tolerance"):
        make().handle(BODY, signed(BODY, ts=ts))


def test_duplicate_event_acknowledged_but_not_reprocessed():
    calls = []
    rx = make()
    rx.on("*", calls.append)
    assert rx.handle(BODY, signed(BODY)).duplicate is False
    assert rx.handle(BODY, signed(BODY)).duplicate is True
    assert len(calls) == 1


def test_failed_handler_does_not_mark_event_seen():
    attempts = []

    def flaky(event):
        attempts.append(event["id"])
        if len(attempts) == 1:
            raise RuntimeError("downstream CRM down")
        return "ok"

    rx = make()
    rx.on("ticket.created", flaky)
    with pytest.raises(RuntimeError):
        rx.handle(BODY, signed(BODY))
    res = rx.handle(BODY, signed(BODY))  # sender retries
    assert (res.duplicate, res.result, len(attempts)) == (False, "ok", 2)


def test_secret_rotation_accepts_old_and_new(monkeypatch):
    monkeypatch.setenv("WH_SECRETS", "whsec_new, whsec_old")
    rx = WebhookReceiver(secret_env="WH_SECRETS", clock=lambda: NOW)
    rx.verify(BODY, signed(BODY, secret="whsec_old"))
    rx.verify(BODY, signed(BODY, secret="whsec_new"))


def test_requires_a_secret(monkeypatch):
    monkeypatch.delenv("WH_SECRETS", raising=False)
    with pytest.raises(ValueError):
        WebhookReceiver(secret_env="WH_SECRETS")


@pytest.mark.parametrize("body", [b"not json", b'["a"]', b'{"type":"x"}'])
def test_bad_payloads_rejected_with_400(body):
    with pytest.raises(WebhookError) as e:
        make().handle(body, signed(body))
    assert e.value.status == 400


def test_unhandled_type_still_acknowledged():
    res = make().handle(BODY, signed(BODY))
    assert res.status == 200 and res.result is None


def test_replay_memory_is_bounded():
    rx = make(max_remembered=2)
    for i in range(3):
        b = json.dumps({"id": f"e{i}", "type": "t"}).encode()
        rx.handle(b, signed(b))
    first = json.dumps({"id": "e0", "type": "t"}).encode()
    assert rx.handle(first, signed(first)).duplicate is False
