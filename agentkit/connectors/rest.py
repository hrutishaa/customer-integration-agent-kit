"""REST connector with timeouts, env-var auth and retry/backoff."""
from __future__ import annotations

import email.utils
import logging
import os
import random
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from agentkit.connectors.base import Connector, ConnectorError, Record

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: float = 0.1
    retry_status: frozenset[int] = field(default=RETRYABLE_STATUS)

    def backoff(self, attempt: int) -> float:
        """Exponential backoff for attempt number ``attempt`` (1-based)."""
        delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return delay + random.uniform(0, self.jitter * delay)


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0.0, dt.timestamp() - now)


class RestConnector(Connector):
    """Talk to a customer's JSON REST API.

    * Auth token is read from an environment variable (never hard-coded).
    * 429/5xx and network errors are retried with exponential backoff,
      honoring ``Retry-After``. Non-idempotent methods (POST/PATCH) are only
      retried when the caller supplies an ``idempotency_key``.
    """

    name = "rest"

    def __init__(
        self,
        base_url: str,
        *,
        token_env: str | None = None,
        auth_scheme: str = "Bearer",
        timeout: float = 10.0,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        data_key: str | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": "agentkit/0.1"}
        if token_env:
            token = os.environ.get(token_env)
            if not token:
                raise ConnectorError(f"environment variable {token_env} is not set")
            headers["Authorization"] = f"{auth_scheme} {token}"
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self.data_key = data_key
        self._client = httpx.Client(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )

    def request(
        self, method: str, path: str, *, idempotency_key: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        method = method.upper()
        headers = dict(kwargs.pop("headers", None) or {})
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        retryable = method in IDEMPOTENT_METHODS or idempotency_key is not None
        attempts = self.retry.max_attempts if retryable else 1

        for attempt in range(1, attempts + 1):
            try:
                resp = self._client.request(method, path, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= attempts:
                    raise ConnectorError(f"{method} {path} failed: {exc}") from exc
                delay = self.retry.backoff(attempt)
                log.warning("%s %s network error (%s); retry in %.2fs", method, path, exc, delay)
                self._sleep(delay)
                continue

            if resp.status_code in self.retry.retry_status and attempt < attempts:
                delay = parse_retry_after(resp.headers.get("Retry-After"))
                if delay is None:
                    delay = self.retry.backoff(attempt)
                delay = min(delay, self.retry.max_delay)
                log.warning("%s %s -> %s; retry in %.2fs", method, path, resp.status_code, delay)
                self._sleep(delay)
                continue

            if resp.is_error:
                raise ConnectorError(
                    f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}"
                )
            return resp
        raise ConnectorError(f"{method} {path} exhausted retries")  # pragma: no cover

    def read(self, resource: str, **params: Any) -> Iterator[Record]:
        data = self.request("GET", resource, params=params or None).json()
        if self.data_key and isinstance(data, dict):
            data = data.get(self.data_key, [])
        if isinstance(data, dict):
            yield data
        else:
            yield from data

    def write(
        self, resource: str, records: Iterable[Record], *, idempotency_prefix: str | None = None
    ) -> int:
        count = 0
        for i, rec in enumerate(records):
            key = f"{idempotency_prefix}-{i}" if idempotency_prefix else None
            self.request("POST", resource, json=rec, idempotency_key=key)
            count += 1
        return count

    def health_check(self) -> bool:
        try:
            self.request("GET", "/")
            return True
        except ConnectorError:
            return False

    def close(self) -> None:
        self._client.close()
