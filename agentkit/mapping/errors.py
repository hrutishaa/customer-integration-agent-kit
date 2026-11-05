"""Errors and per-record reports for schema mapping."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentkit.connectors.base import Record


class MappingError(Exception):
    """A mapping spec is invalid, or ``apply(..., on_error='raise')`` found bad rows.

    ``errors`` is the per-record report (empty when the spec itself is invalid).
    ``report`` is the full :class:`agentkit.mapping.MappingReport` when a batch
    was applied and then rejected.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[RecordError] | None = None,
        report: Any = None,
    ) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.report = report


class CastError(MappingError):
    """A value cannot be cast to the declared type."""


class ExprError(MappingError):
    """A compute expression is invalid or failed on a value."""


@dataclass(frozen=True)
class FieldError:
    """One problem with one output field of one source row."""

    field: str
    message: str
    value: Any = None
    kind: str = "validation"  # cast | required | compute | validation | record

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


@dataclass(frozen=True)
class RecordError:
    """Every field problem for a single source row. ``index`` is 0-based."""

    index: int
    errors: tuple[FieldError, ...]
    source: Record | None = None

    def __str__(self) -> str:
        detail = "; ".join(str(err) for err in self.errors)
        return f"record {self.index}: {detail}"


def failure_message(errors: list[RecordError]) -> str:
    if not errors:
        return "mapping failed"
    extra = len(errors) - 1
    if extra:
        suffix = "s" if extra != 1 else ""
        return f"{errors[0]} (+{extra} more record{suffix})"
    return str(errors[0])
