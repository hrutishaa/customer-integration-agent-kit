"""SQL connector: read/write customer databases (SQLite, Postgres) via SQLAlchemy Core.

Design choices that matter at a customer site:

* The database URL comes from an env var, so credentials never land in code or logs.
* Table and column names are checked against the live schema (reflection), and every
  value is a bound parameter, so neither an LLM nor a config file can inject SQL.
* ``read`` streams rows in keyset-paginated batches, so a 5M-row table never sits in memory.
* ``write`` runs in one transaction (all rows or none) and can upsert on a key column.
* ``read_only=True`` blocks every write, the safe default when an agent explores a
  customer's production replica.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from typing import Any

from agentkit.connectors.base import Connector, ConnectorError, Record

try:  # optional dependency: pip install "customer-integration-agent-kit[sql]"
    import sqlalchemy as sa
    from sqlalchemy.exc import SQLAlchemyError
except ImportError:  # pragma: no cover - exercised only without the extra
    sa = None  # type: ignore[assignment]
    SQLAlchemyError = Exception  # type: ignore[assignment,misc]


class SQLConnector(Connector):
    """Treat each table in a SQL database as a resource."""

    name = "sql"

    def __init__(
        self,
        url: str | None = None,
        *,
        url_env: str | None = None,
        read_only: bool = False,
        batch_size: int = 500,
        engine: Any = None,
    ) -> None:
        if sa is None:
            raise ConnectorError('SQLConnector needs SQLAlchemy: pip install ".[sql]"')
        if engine is None:
            if url is None and url_env:
                url = os.environ.get(url_env)
                if not url:
                    raise ConnectorError(f"env var {url_env} is not set")
            if not url:
                raise ConnectorError("pass url=, url_env= or engine=")
            engine = sa.create_engine(url, future=True)
        if batch_size < 1:
            raise ConnectorError("batch_size must be >= 1")
        self.engine = engine
        self.read_only = read_only
        self.batch_size = batch_size
        self._meta = sa.MetaData()

    # -- schema -----------------------------------------------------------------
    def tables(self) -> list[str]:
        """Table names that exist right now (what an agent may read)."""
        return sorted(sa.inspect(self.engine).get_table_names())

    def _table(self, resource: str) -> Any:
        if resource in self._meta.tables:
            return self._meta.tables[resource]
        if resource not in self.tables():
            raise ConnectorError(f"no such table: {resource!r}")
        return sa.Table(resource, self._meta, autoload_with=self.engine)

    def schema(self, resource: str) -> dict[str, str]:
        """Column name -> SQL type, e.g. to describe a table to an LLM."""
        return {c.name: str(c.type) for c in self._table(resource).columns}

    def _columns(self, table: Any, names: Iterable[str], what: str) -> None:
        unknown = sorted(set(names) - set(table.c.keys()))
        if unknown:
            raise ConnectorError(f"unknown {what} for {table.name}: {unknown}")

    # -- read -------------------------------------------------------------------
    def read(
        self,
        resource: str,
        *,
        columns: list[str] | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        **filters: Any,
    ) -> Iterator[Record]:
        """Yield rows matching ``column=value`` filters (``None`` means IS NULL).

        Rows are fetched in batches of ``batch_size`` with keyset pagination on
        the primary key, so memory stays flat and pages
        don't shift when rows are inserted mid-read.
        """
        table = self._table(resource)
        self._columns(table, filters, "filter columns")
        if columns:
            self._columns(table, columns, "columns")
        key_name = order_by or self._default_key(table)
        self._columns(table, [key_name], "order_by column")
        key = table.c[key_name]
        if not key.primary_key:
            # Keyset paging needs a unique, non-null key; other sort columns
            # (including nullable UNIQUE ones) are read in a single query.
            yield from self._read_once(table, columns, filters, key, limit)
            return

        selected = [table.c[c] for c in columns] if columns else list(table.c)
        if key not in selected:
            selected.append(key)
        where = [self._cond(table, k, v) for k, v in filters.items()]
        last: Any = _START
        remaining = limit
        while remaining is None or remaining > 0:
            size = self.batch_size if remaining is None else min(self.batch_size, remaining)
            stmt = sa.select(*selected).where(*where).order_by(key).limit(size)
            if last is not _START:
                stmt = stmt.where(key > last)
            rows = self._execute(stmt).mappings().all()
            for row in rows:
                record = dict(row)
                last = record[key_name]
                if columns and key_name not in columns:
                    record.pop(key_name)
                yield record
            if remaining is not None:
                remaining -= len(rows)
            if len(rows) < size:
                return

    def _read_once(self, table, columns, filters, key, limit) -> Iterator[Record]:
        selected = [table.c[c] for c in columns] if columns else list(table.c)
        stmt = sa.select(*selected).where(
            *[self._cond(table, k, v) for k, v in filters.items()]
        ).order_by(key)
        if limit is not None:
            stmt = stmt.limit(limit)
        for row in self._execute(stmt).mappings():
            yield dict(row)

    @staticmethod
    def _cond(table: Any, column: str, value: Any) -> Any:
        col = table.c[column]
        return col.is_(None) if value is None else col == value

    @staticmethod
    def _default_key(table: Any) -> str:
        pk = list(table.primary_key.columns)
        if len(pk) != 1:
            raise ConnectorError(
                f"{table.name} has no single-column primary key; pass order_by="
            )
        return pk[0].name

    def query(self, sql: str, **params: Any) -> list[Record]:
        """Run a read-only SELECT with bound ``:name`` parameters.

        Only one SELECT/WITH statement is allowed; values must be passed as params,
        never formatted into the string.
        """
        stripped = sql.strip().rstrip(";")
        if ";" in stripped or stripped.split(None, 1)[0].lower() not in {"select", "with"}:
            raise ConnectorError("query() accepts a single SELECT statement")
        return [dict(r) for r in self._execute(sa.text(stripped), params).mappings()]

    # -- write ------------------------------------------------------------------
    def write(
        self, resource: str, records: Iterable[Record], *, upsert_on: str | None = None
    ) -> int:
        """Insert (or upsert on ``upsert_on``) records in one transaction."""
        if self.read_only:
            raise ConnectorError("connector is read-only")
        rows = list(records)
        if not rows:
            return 0
        table = self._table(resource)
        self._columns(table, {k for r in rows for k in r}, "columns")
        if upsert_on is None:
            stmt = sa.insert(table)
        else:
            stmt = self._upsert(table, rows, upsert_on)
        try:
            with self.engine.begin() as conn:
                conn.execute(stmt, rows)
        except SQLAlchemyError as exc:
            raise ConnectorError(f"write to {resource} failed, rolled back: {exc.orig or exc}")
        return len(rows)

    def _upsert(self, table: Any, rows: list[Record], key: str) -> Any:
        self._columns(table, [key], "upsert_on column")
        if any(key not in r for r in rows):
            raise ConnectorError(f"every record needs {key!r} to upsert")
        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise ConnectorError(f"upsert not supported on {dialect}")
        stmt = insert(table)
        updates = {c: stmt.excluded[c] for c in rows[0] if c != key}
        if not updates:
            return stmt.on_conflict_do_nothing(index_elements=[key])
        return stmt.on_conflict_do_update(index_elements=[key], set_=updates)

    # -- plumbing ---------------------------------------------------------------
    def _execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        try:
            with self.engine.connect() as conn:
                result = conn.execute(stmt, params or {})
                return _Buffered(result.mappings().all())
        except SQLAlchemyError as exc:
            raise ConnectorError(f"query failed: {getattr(exc, 'orig', None) or exc}")

    def health_check(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def close(self) -> None:
        self.engine.dispose()


_START = object()


class _Buffered:
    """Rows already fetched, so the connection can close before we yield."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> _Buffered:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)
