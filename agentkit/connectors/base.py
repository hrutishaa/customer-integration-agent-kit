"""Base connector interface shared by every customer-system adapter."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Any, Self

Record = dict[str, Any]


class ConnectorError(Exception):
    """Raised when a connector cannot complete an operation."""


class Connector(ABC):
    """A uniform read/write interface over a customer system.

    Agents and pipelines talk to connectors, never to raw systems, so a
    customer's REST API, CSV export or database can be swapped freely.
    """

    name: str = "connector"

    @abstractmethod
    def read(self, resource: str, **params: Any) -> Iterator[Record]:
        """Yield records from ``resource`` (an endpoint, file, table...)."""

    @abstractmethod
    def write(self, resource: str, records: Iterable[Record]) -> int:
        """Write records to ``resource``; return the number written."""

    def health_check(self) -> bool:
        """Return True if the system is reachable. Override when cheap to check."""
        return True

    def close(self) -> None:
        """Release resources (connections, file handles)."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
