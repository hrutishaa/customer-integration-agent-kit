"""A mock "Acme CRM" API that behaves like a real, slightly hostile SaaS CRM.

It runs in-process behind ``httpx.MockTransport`` so tests and demos need no
network. It deliberately has the quirks a forward-deployed engineer meets:

* Bearer-token auth (401 without it).
* Cursor pagination with opaque cursors, ordered by ``(updated_at, id)`` so
  records edited mid-sync are not skipped.
* ``updated_since`` filter for incremental syncs.
* A rate limit: every ``rate_limit_every``-th request gets ``429`` with
  ``Retry-After``.
* ``Idempotency-Key`` on POST: a replayed key returns the original response
  instead of creating a duplicate.
* Unique emails on contacts (``409`` on conflict).
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

MAX_LIMIT = 100
WRITABLE_FIELDS = {"email", "name", "company", "phone", "stage", "amount", "owner"}


def _encode_cursor(updated_at: str, rid: int) -> str:
    raw = json.dumps([updated_at, rid]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    pad = "=" * (-len(cursor) % 4)
    updated_at, rid = json.loads(base64.urlsafe_b64decode(cursor + pad))
    return str(updated_at), int(rid)


class AcmeCRM:
    """In-memory CRM with ``contacts`` and ``deals`` resources."""

    resources = ("contacts", "deals")

    def __init__(self, token: str = "acme-test-token", *, rate_limit_every: int = 0) -> None:
        self.token = token
        self.rate_limit_every = rate_limit_every
        self.requests = 0
        self.rate_limited = 0
        self._data: dict[str, dict[int, dict[str, Any]]] = {r: {} for r in self.resources}
        self._next_id = 1
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)
        self._idem: dict[str, tuple[int, dict[str, Any]]] = {}

    # ----- helpers -------------------------------------------------------
    def _tick(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    def seed(self, resource: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert records directly (bypassing the API); return the stored rows."""
        return [self._insert(resource, dict(r)) for r in records]

    def _insert(self, resource: str, fields: dict[str, Any]) -> dict[str, Any]:
        rec = {"id": self._next_id, **fields, "updated_at": self._tick()}
        self._data[resource][self._next_id] = rec
        self._next_id += 1
        return dict(rec)

    def update(self, resource: str, rid: int, **fields: Any) -> dict[str, Any]:
        """Simulate a user editing a record in the CRM UI."""
        rec = self._data[resource][rid]
        rec.update(fields, updated_at=self._tick())
        return dict(rec)

    def all(self, resource: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._data[resource].values()]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ----- HTTP ----------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.rate_limit_every and self.requests % self.rate_limit_every == 0:
            self.rate_limited += 1
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "rate_limited"})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})

        parts = [p for p in request.url.path.split("/") if p]
        if parts == ["v1", "ping"]:
            return httpx.Response(200, json={"ok": True})
        if len(parts) < 2 or parts[0] != "v1" or parts[1] not in self.resources:
            return httpx.Response(404, json={"error": "not_found"})
        resource, rest = parts[1], parts[2:]
        method = request.method

        if method == "GET" and not rest:
            return self._list(resource, request.url.params)
        if method == "GET" and rest == ["search"]:
            return self._search(resource, request.url.params)
        if method == "POST" and not rest:
            return self._create(resource, request)
        if method == "PATCH" and len(rest) == 1 and rest[0].isdigit():
            return self._patch(resource, int(rest[0]), request)
        return httpx.Response(405, json={"error": "method_not_allowed"})

    def _list(self, resource: str, params: httpx.QueryParams) -> httpx.Response:
        try:
            limit = int(params.get("limit", "25"))
        except ValueError:
            return httpx.Response(400, json={"error": "bad_limit"})
        if not 1 <= limit <= MAX_LIMIT:
            return httpx.Response(400, json={"error": f"limit must be 1..{MAX_LIMIT}"})
        rows = sorted(self._data[resource].values(), key=lambda r: (r["updated_at"], r["id"]))
        since = params.get("updated_since")
        if since:
            rows = [r for r in rows if r["updated_at"] > since]
        cursor = params.get("cursor")
        if cursor:
            try:
                after = _decode_cursor(cursor)
            except (ValueError, TypeError):
                return httpx.Response(400, json={"error": "invalid_cursor"})
            rows = [r for r in rows if (r["updated_at"], r["id"]) > after]
        page = rows[:limit]
        has_more = len(rows) > limit
        nxt = _encode_cursor(page[-1]["updated_at"], page[-1]["id"]) if has_more else None
        return httpx.Response(200, json={"data": [dict(r) for r in page], "next_cursor": nxt})

    def _search(self, resource: str, params: httpx.QueryParams) -> httpx.Response:
        filters = {k: v for k, v in params.items() if k in WRITABLE_FIELDS}
        if not filters:
            return httpx.Response(400, json={"error": "no_filters"})
        hits = [
            dict(r)
            for r in self._data[resource].values()
            if all(str(r.get(k, "")).lower() == v.lower() for k, v in filters.items())
        ]
        return httpx.Response(200, json={"data": hits, "next_cursor": None})

    def _body(self, request: httpx.Request) -> dict[str, Any] | httpx.Response:
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return httpx.Response(400, json={"error": "invalid_json"})
        unknown = set(body) - WRITABLE_FIELDS
        if unknown:
            return httpx.Response(422, json={"error": f"unknown fields: {sorted(unknown)}"})
        return body

    def _create(self, resource: str, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        if key and key in self._idem:
            status, payload = self._idem[key]
            return httpx.Response(status, json=payload, headers={"Idempotent-Replay": "true"})
        body = self._body(request)
        if isinstance(body, httpx.Response):
            return body
        email = body.get("email")
        if resource == "contacts" and email and any(
            str(r.get("email", "")).lower() == email.lower() for r in self._data[resource].values()
        ):
            return httpx.Response(409, json={"error": "email_exists"})
        rec = self._insert(resource, body)
        if key:
            self._idem[key] = (201, rec)
        return httpx.Response(201, json=rec)

    def _patch(self, resource: str, rid: int, request: httpx.Request) -> httpx.Response:
        if rid not in self._data[resource]:
            return httpx.Response(404, json={"error": "not_found"})
        body = self._body(request)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=self.update(resource, rid, **body))
