import httpx
import pytest

from agentkit import ConnectorError, CRMConnector, RetryPolicy
from agentkit.mock import AcmeCRM

TOKEN = "acme-test-token"


@pytest.fixture
def crm():
    return AcmeCRM(token=TOKEN)


def connect(crm, monkeypatch, sleeps=None, **kw):
    monkeypatch.setenv("ACME_TOKEN", TOKEN)
    return CRMConnector(
        "https://acme.test",
        token_env="ACME_TOKEN",
        transport=crm.transport,
        sleep=(sleeps if sleeps is not None else []).append,
        retry=RetryPolicy(max_attempts=3, base_delay=0.01, jitter=0),
        **kw,
    )


def contacts(n):
    return [{"email": f"user{i}@acme.com", "name": f"User {i}"} for i in range(n)]


def test_reads_every_page_in_order(crm, monkeypatch):
    crm.seed("contacts", contacts(23))
    conn = connect(crm, monkeypatch, page_size=5)
    pages = list(conn.pages("contacts"))
    assert [len(p) for p in pages] == [5, 5, 5, 5, 3]
    ids = [r["id"] for r in conn.read("contacts")]
    assert ids == list(range(1, 24))


def test_empty_resource_is_one_empty_page(crm, monkeypatch):
    assert list(connect(crm, monkeypatch).pages("deals")) == [[]]


def test_missing_token_gets_401(crm, monkeypatch):
    monkeypatch.setenv("ACME_TOKEN", "wrong")
    conn = CRMConnector("https://acme.test", token_env="ACME_TOKEN", transport=crm.transport)
    with pytest.raises(ConnectorError, match="401"):
        list(conn.read("contacts"))


def test_rate_limit_429_is_retried_with_retry_after(crm, monkeypatch):
    crm.rate_limit_every = 3
    crm.seed("contacts", contacts(10))
    sleeps = []
    rows = list(connect(crm, monkeypatch, sleeps=sleeps, page_size=2).read("contacts"))
    assert len(rows) == 10
    assert crm.rate_limited >= 1
    assert sleeps and all(s == 1.0 for s in sleeps)


def test_incremental_sync_only_returns_changes(crm, monkeypatch):
    crm.seed("contacts", contacts(4))
    conn = connect(crm, monkeypatch, page_size=2)
    first = conn.sync("contacts")
    assert len(first.records) == 4 and first.pages == 2

    nothing = conn.sync("contacts", since=first.watermark)
    assert nothing.records == [] and nothing.watermark == first.watermark

    crm.update("contacts", 2, name="Renamed")
    crm.seed("contacts", [{"email": "new@acme.com", "name": "New"}])
    delta = conn.sync("contacts", since=first.watermark)
    assert sorted(r["id"] for r in delta.records) == [2, 5]
    assert delta.watermark > first.watermark


def test_record_edited_mid_sync_is_not_lost(crm, monkeypatch):
    crm.seed("contacts", contacts(6))
    conn = connect(crm, monkeypatch, page_size=2)
    seen = []
    for i, page in enumerate(conn.pages("contacts")):
        seen += [r["id"] for r in page]
        if i == 0:
            crm.update("contacts", 1, name="edited while paging")
    # keyset on (updated_at, id): the edited row moves to the end and is re-read
    assert set(seen) == set(range(1, 7))
    assert seen.count(1) == 2


def test_repeated_cursor_raises_instead_of_looping(monkeypatch):
    monkeypatch.setenv("T", "x")

    def stuck(req):
        return httpx.Response(200, json={"data": [{"id": 1}], "next_cursor": "same"})

    conn = CRMConnector("https://bad.test", token_env="T", transport=httpx.MockTransport(stuck))
    with pytest.raises(ConnectorError, match="repeated cursor"):
        list(conn.read("contacts"))


def test_max_pages_guard(crm, monkeypatch):
    crm.seed("contacts", contacts(5))
    with pytest.raises(ConnectorError, match="max_pages"):
        list(connect(crm, monkeypatch, page_size=1, max_pages=3).read("contacts"))


def test_bad_page_shape_raises(monkeypatch):
    monkeypatch.setenv("T", "x")
    conn = CRMConnector(
        "https://bad.test",
        token_env="T",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 2])),
    )
    with pytest.raises(ConnectorError, match="unexpected page shape"):
        list(conn.read("contacts"))


def test_upsert_creates_then_updates_without_duplicates(crm, monkeypatch):
    conn = connect(crm, monkeypatch)
    batch = [{"email": "a@acme.com", "name": "A"}, {"email": "b@acme.com", "name": "B"}]
    assert conn.write("contacts", batch, upsert_on="email") == 2
    batch[0]["name"] = "A (updated)"
    assert conn.write("contacts", batch, upsert_on="email") == 2
    rows = {r["email"]: r for r in crm.all("contacts")}
    assert len(rows) == 2
    assert rows["a@acme.com"]["name"] == "A (updated)"


def test_upsert_skips_patch_when_nothing_changed(crm, monkeypatch):
    crm.seed("contacts", [{"email": "a@acme.com", "name": "A"}])
    before = crm.all("contacts")[0]["updated_at"]
    connect(crm, monkeypatch).write("contacts", [{"email": "a@acme.com", "name": "A"}],
                                    upsert_on="email")
    assert crm.all("contacts")[0]["updated_at"] == before


def test_upsert_refuses_ambiguous_match(crm, monkeypatch):
    crm.seed("deals", [{"name": "Renewal", "amount": 1}, {"name": "Renewal", "amount": 2}])
    with pytest.raises(ConnectorError, match="refusing to guess"):
        connect(crm, monkeypatch).write("deals", [{"name": "Renewal", "amount": 3}],
                                        upsert_on="name")


def test_upsert_requires_key(crm, monkeypatch):
    with pytest.raises(ConnectorError, match="missing upsert key"):
        connect(crm, monkeypatch).write("contacts", [{"name": "No email"}], upsert_on="email")


def test_rerun_post_is_not_duplicated(crm, monkeypatch):
    """A crashed-and-rerun sync sends the same Idempotency-Key, so no duplicate."""
    conn = connect(crm, monkeypatch)
    payload = {"name": "Big deal", "amount": 5000}
    conn.write("deals", [payload])
    conn.write("deals", [payload])  # same key, API replays the original
    assert len(crm.all("deals")) == 1
    assert CRMConnector.idempotency_key("deals", payload) == CRMConnector.idempotency_key(
        "deals", dict(reversed(list(payload.items())))
    )


def test_unknown_fields_are_rejected_by_api(crm, monkeypatch):
    with pytest.raises(ConnectorError, match="422"):
        connect(crm, monkeypatch).write("contacts", [{"email": "x@acme.com", "ssn": "1"}])


def test_invalid_cursor_from_caller_is_400(crm, monkeypatch):
    with pytest.raises(ConnectorError, match="400"):
        list(connect(crm, monkeypatch).read("contacts", cursor="!!notbase64!!"))


def test_resource_name_is_validated(crm, monkeypatch):
    with pytest.raises(ConnectorError, match="invalid resource"):
        list(connect(crm, monkeypatch).read("../admin"))


def test_health_check(crm, monkeypatch):
    assert connect(crm, monkeypatch).health_check() is True
    monkeypatch.setenv("ACME_TOKEN", "nope")
    bad = CRMConnector("https://acme.test", token_env="ACME_TOKEN", transport=crm.transport)
    assert bad.health_check() is False
