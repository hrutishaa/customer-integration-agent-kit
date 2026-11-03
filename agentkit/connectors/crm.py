"""CRM connector: cursor pagination, incremental sync and idempotent upserts.

Built on :class:`RestConnector`, so auth from an env var, timeouts and
429/5xx backoff come for free. Works against any CRM that pages with an
opaque cursor (``{"data": [...], "next_cursor": "..."}``); the bundled
:class:`agentkit.mock.AcmeCRM` is the reference implementation.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from agentkit.connectors.base import Connector, ConnectorError, Record
from agentkit.connectors.rest import RestConnector, RetryPolicy


@dataclass
class SyncResult:
    """Outcome of an incremental sync: the records and the next watermark."""

    records: list[Record]
    watermark: str | None
    pages: int


class CRMConnector(Connector):
    """Read and write a cursor-paginated CRM API.

    * ``read`` follows ``next_cursor`` until the last page and stops with an
      error if the API ever repeats a cursor (a real bug seen in the wild that
      would otherwise loop forever).
    * ``sync`` pulls only records changed since a watermark and returns the
      new watermark to store for the next run.
    * ``write(..., upsert_on="email")`` searches first, PATCHes a match or
      POSTs a new record with a deterministic ``Idempotency-Key``, so a sync
      that crashes and is re-run never creates duplicates.
    """

    name = "crm"

    def __init__(
        self,
        base_url: str,
        *,
        token_env: str | None = None,
        api_prefix: str = "/v1",
        page_size: int = 100,
        max_pages: int = 10_000,
        cursor_param: str = "cursor",
        timeout: float = 10.0,
        retry: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        kwargs: dict[str, Any] = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        self._rest = RestConnector(
            base_url,
            token_env=token_env,
            timeout=timeout,
            retry=retry,
            transport=transport,
            **kwargs,
        )
        self.prefix = "/" + api_prefix.strip("/") if api_prefix.strip("/") else ""
        self.page_size = page_size
        self.max_pages = max_pages
        self.cursor_param = cursor_param

    def _path(self, resource: str, *rest: str | int) -> str:
        resource = resource.strip("/")
        if not resource or "/" in resource or ".." in resource:
            raise ConnectorError(f"invalid resource name: {resource!r}")
        return "/".join([self.prefix, resource, *(str(r) for r in rest)])

    # ----- reads ---------------------------------------------------------
    def pages(self, resource: str, **params: Any) -> Iterator[list[Record]]:
        """Yield one list of records per API page."""
        params = {"limit": self.page_size, **params}
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(self.max_pages):
            q = dict(params)
            if cursor:
                q[self.cursor_param] = cursor
            body = self._rest.request("GET", self._path(resource), params=q).json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise ConnectorError(f"unexpected page shape from {resource}: {str(body)[:120]}")
            yield body["data"]
            cursor = body.get("next_cursor")
            if not cursor:
                return
            if cursor in seen:
                raise ConnectorError(f"{resource}: API repeated cursor {cursor!r}; stopping")
            seen.add(cursor)
        raise ConnectorError(f"{resource}: exceeded max_pages={self.max_pages}")

    def read(self, resource: str, **params: Any) -> Iterator[Record]:
        for page in self.pages(resource, **params):
            yield from page

    def sync(
        self, resource: str, since: str | None = None, *, field: str = "updated_at"
    ) -> SyncResult:
        """Incremental pull of records changed after ``since``.

        Store ``result.watermark`` and pass it back next run. If nothing
        changed, the old watermark is returned unchanged.
        """
        params = {"updated_since": since} if since else {}
        records: list[Record] = []
        pages = 0
        for page in self.pages(resource, **params):
            records.extend(page)
            pages += 1
        stamps = [str(r[field]) for r in records if r.get(field)]
        watermark = max(stamps, default=since)
        return SyncResult(records=records, watermark=watermark, pages=pages)

    def find(self, resource: str, **filters: Any) -> list[Record]:
        body = self._rest.request("GET", self._path(resource, "search"), params=filters).json()
        return list(body.get("data", []))

    # ----- writes --------------------------------------------------------
    @staticmethod
    def idempotency_key(resource: str, record: Record) -> str:
        """Same resource + same payload -> same key, across process restarts."""
        canon = repr(sorted(record.items()))
        return hashlib.sha256(f"{resource}:{canon}".encode()).hexdigest()[:32]

    def write(
        self, resource: str, records: Iterable[Record], *, upsert_on: str | None = None
    ) -> int:
        count = 0
        for rec in records:
            payload = {k: v for k, v in rec.items() if k not in {"id", "updated_at"}}
            if upsert_on:
                if payload.get(upsert_on) in (None, ""):
                    raise ConnectorError(f"record missing upsert key {upsert_on!r}: {rec}")
                matches = self.find(resource, **{upsert_on: payload[upsert_on]})
                if len(matches) > 1:
                    raise ConnectorError(
                        f"{len(matches)} {resource} match {upsert_on}={payload[upsert_on]!r}; "
                        "refusing to guess which to update"
                    )
                if matches:
                    current = matches[0]
                    changed = {k: v for k, v in payload.items() if current.get(k) != v}
                    if changed:
                        self._rest.request(
                            "PATCH",
                            self._path(resource, current["id"]),
                            json=changed,
                            idempotency_key=self.idempotency_key(resource, changed),
                        )
                    count += 1
                    continue
            self._rest.request(
                "POST",
                self._path(resource),
                json=payload,
                idempotency_key=self.idempotency_key(resource, payload),
            )
            count += 1
        return count

    def health_check(self) -> bool:
        try:
            self._rest.request("GET", f"{self.prefix}/ping")
            return True
        except ConnectorError:
            return False

    def close(self) -> None:
        self._rest.close()
