"""CSV connector: read/write customer exports in a local directory."""
from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from agentkit.connectors.base import Connector, ConnectorError, Record


class CSVConnector(Connector):
    """Treat each ``<resource>.csv`` file in ``root`` as a table."""

    name = "csv"

    def __init__(self, root: str | Path, *, encoding: str = "utf-8") -> None:
        self.root = Path(root)
        self.encoding = encoding

    def _path(self, resource: str) -> Path:
        name = resource if resource.endswith(".csv") else f"{resource}.csv"
        path = (self.root / name).resolve()
        if self.root.resolve() not in path.parents:
            raise ConnectorError(f"resource {resource!r} escapes connector root")
        return path

    def read(self, resource: str, **filters: Any) -> Iterator[Record]:
        path = self._path(resource)
        if not path.exists():
            raise ConnectorError(f"no such resource: {resource}")
        with path.open(newline="", encoding=self.encoding) as fh:
            for row in csv.DictReader(fh):
                if all(row.get(k) == str(v) for k, v in filters.items()):
                    yield row

    def write(self, resource: str, records: Iterable[Record], *, append: bool = True) -> int:
        rows = list(records)
        if not rows:
            return 0
        path = self._path(resource)
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0
        if append and exists:
            with path.open(newline="", encoding=self.encoding) as fh:
                fieldnames = next(csv.reader(fh))
        else:
            fieldnames = list(dict.fromkeys(k for r in rows for k in r))
        extra = {k for r in rows for k in r} - set(fieldnames)
        if extra:
            raise ConnectorError(f"unknown columns for {resource}: {sorted(extra)}")
        mode = "a" if append and exists else "w"
        with path.open(mode, newline="", encoding=self.encoding) as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            if mode == "w":
                writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def health_check(self) -> bool:
        return self.root.is_dir()
