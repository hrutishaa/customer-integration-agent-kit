"""Inbound webhook receiver: HMAC verification over the raw body plus replay protection.

Customer systems (CRMs, ticketing tools, payment providers) push events to us as
signed HTTP POSTs. Getting this right is mostly about a few sharp edges:

* The signature must be computed over the **raw request bytes**. Re-serialising
  parsed JSON changes whitespace, key order, unicode escapes and number formats,
  so valid events start failing (or, worse, checks get disabled "temporarily").
* Signatures are compared in constant time (``hmac.compare_digest``).
* A timestamp is signed together with the body, and old timestamps are rejected,
  so a captured request can't be replayed next week.
* Event IDs are remembered **only after the handler succeeds**. If a handler
  fails, the sender's retry is processed again instead of being dropped.
* Several secrets can be active at once, so secrets can be rotated without downtime.

Header format (Stripe-style): ``X-Signature: t=<unix seconds>,v1=<hex hmac-sha256>``
where the signed payload is ``f"{t}.".encode() + raw_body``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from agentkit.connectors.base import ConnectorError

Handler = Callable[[dict[str, Any]], Any]


class WebhookError(ConnectorError):
    """Raised for a webhook that must be rejected. ``status`` is the HTTP code to return."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WebhookResult:
    status: int
    event_id: str | None = None
    duplicate: bool = False
    result: Any = None


def sign(secret: str | bytes, body: bytes, timestamp: int | None = None) -> str:
    """Build a signature header value for ``body``. Useful for tests and mock senders."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    key = secret.encode() if isinstance(secret, str) else secret
    digest = hmac.new(key, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


class _SeenStore:
    """Bounded, TTL-based memory of processed event IDs (thread-safe)."""

    def __init__(self, ttl: float, max_size: int) -> None:
        self.ttl, self.max_size = ttl, max_size
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        while self._seen and (
            len(self._seen) > self.max_size or next(iter(self._seen.values())) < now - self.ttl
        ):
            self._seen.popitem(last=False)

    def __contains__(self, event_id: str) -> bool:
        with self._lock:
            self._evict(time.time())
            return event_id in self._seen

    def add(self, event_id: str) -> None:
        with self._lock:
            self._seen[event_id] = time.time()
            self._seen.move_to_end(event_id)
            self._evict(time.time())


class WebhookReceiver:
    """Verify, de-duplicate and dispatch signed webhook events.

    >>> rx = WebhookReceiver(secrets=["whsec_test"])
    >>> rx.on("ticket.created", lambda e: e["data"]["id"])  # doctest: +SKIP
    >>> rx.handle(raw_body, headers)  # returns WebhookResult(status=200, ...)  # doctest: +SKIP
    """

    def __init__(
        self,
        secrets: Iterable[str] | None = None,
        *,
        secret_env: str | None = None,
        header: str = "X-Signature",
        tolerance_seconds: int = 300,
        id_field: str = "id",
        type_field: str = "type",
        replay_ttl_seconds: float = 24 * 3600,
        max_remembered: int = 100_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        keys = list(secrets or [])
        if secret_env:
            # comma-separated so an old and a new secret can overlap during rotation
            keys += [s.strip() for s in os.environ.get(secret_env, "").split(",") if s.strip()]
        if not keys:
            raise ValueError("WebhookReceiver needs at least one secret")
        self._keys = [k.encode() for k in keys]
        self.header = header.lower()
        self.tolerance = tolerance_seconds
        self.id_field, self.type_field = id_field, type_field
        self._clock = clock
        self._seen = _SeenStore(replay_ttl_seconds, max_remembered)
        self._handlers: dict[str, Handler] = {}

    def on(self, event_type: str, handler: Handler) -> None:
        """Register ``handler`` for ``event_type`` (``"*"`` catches everything else)."""
        self._handlers[event_type] = handler

    def verify(self, body: bytes, headers: dict[str, str]) -> None:
        """Raise ``WebhookError`` unless ``body`` carries a fresh, valid signature."""
        if not isinstance(body, bytes | bytearray):
            raise TypeError("verify the raw request bytes, not parsed JSON")
        value = {k.lower(): v for k, v in headers.items()}.get(self.header)
        if not value:
            raise WebhookError("missing signature header", 401)
        ts, sigs = None, []
        for part in value.split(","):
            k, _, v = part.strip().partition("=")
            if k == "t":
                ts = v
            elif k == "v1":
                sigs.append(v)
        if ts is None or not ts.isdigit() or not sigs:
            raise WebhookError("malformed signature header", 401)
        if abs(self._clock() - int(ts)) > self.tolerance:
            raise WebhookError("timestamp outside tolerance window", 401)
        signed = f"{ts}.".encode() + bytes(body)
        for key in self._keys:
            expected = hmac.new(key, signed, hashlib.sha256).hexdigest()
            if any(hmac.compare_digest(expected, s) for s in sigs):
                return
        raise WebhookError("invalid signature", 401)

    def handle(self, body: bytes, headers: dict[str, str]) -> WebhookResult:
        """Verify then dispatch. Returns the HTTP status the endpoint should send.

        Duplicates return 200 so the sender stops retrying. Handler failures
        propagate (so the endpoint returns 5xx) and the event is NOT marked seen.
        """
        self.verify(body, headers)
        try:
            event = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookError(f"body is not valid JSON: {exc}") from exc
        if not isinstance(event, dict) or not event.get(self.id_field):
            raise WebhookError(f"event must be an object with an '{self.id_field}' field")
        event_id = str(event[self.id_field])
        if event_id in self._seen:
            return WebhookResult(200, event_id, duplicate=True)
        handler = self._handlers.get(str(event.get(self.type_field)), self._handlers.get("*"))
        result = handler(event) if handler else None
        self._seen.add(event_id)  # only after success, so failed events can be retried
        return WebhookResult(200, event_id, result=result)
