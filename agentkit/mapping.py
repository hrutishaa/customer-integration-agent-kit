"""Declarative schema mapping: customer-shaped records in, your shape out.

A mapping is a small YAML file, so a new customer's column names live in config
instead of code::

    name: contacts_csv
    fields:
      email: {from: "Email Address", transform: [strip, lower], required: true}
      name: {compute: "{First} {Last}", transform: [strip]}
      amount: {from: "Deal Value", cast: float, default: 0}
      stage: {from: Stage, default: lead}

Each target field has exactly one source: ``from`` (rename), ``compute`` (a
``{Field}`` template, never ``eval``) or only a ``default``. Then, in order:
transforms, ``cast``, ``default`` for missing/empty values, and ``required``.
One bad record never aborts the batch: it lands in ``MapResult.errors`` with its
index, field and reason, so the customer gets a fix list instead of a stack trace.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agentkit.connectors.base import ConnectorError, Record

_MISSING = object()
_TEMPLATE_RE = re.compile(r"\{([^{}]+)\}")
_TRUE = {"true", "yes", "y", "1", "t"}
_FALSE = {"false", "no", "n", "0", "f"}


class MappingError(ConnectorError):
    """The mapping file itself is invalid (a config bug, not a bad record)."""


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"cannot read {value!r} as a boolean")


def _to_int(value: Any) -> int:
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{value!r} is not a whole number")
    return int(float(str(value).replace(",", "").strip())) if isinstance(value, str) else int(value)


def _to_float(value: Any) -> float:
    if isinstance(value, str):
        value = value.replace(",", "").replace("$", "").strip()
    return float(value)


def _to_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value).strip()[:10]).isoformat()


CASTS: dict[str, Callable[[Any], Any]] = {
    "str": lambda v: str(v),
    "int": _to_int,
    "float": _to_float,
    "bool": _to_bool,
    "date": _to_date,
}
TRANSFORMS: dict[str, Callable[[str], str]] = {
    "strip": str.strip,
    "lower": str.lower,
    "upper": str.upper,
    "title": str.title,
    "digits": lambda s: re.sub(r"\D", "", s),
    "collapse_spaces": lambda s: re.sub(r"\s+", " ", s).strip(),
}


class FieldSpec(BaseModel):
    """How to build one target field."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str | None = Field(default=None, alias="from")
    compute: str | None = None
    transform: list[Literal["strip", "lower", "upper", "title", "digits", "collapse_spaces"]] = []
    cast: Literal["str", "int", "float", "bool", "date"] | None = None
    default: Any = _MISSING
    required: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> FieldSpec:
        if self.source is not None and self.compute is not None:
            raise ValueError("use either 'from' or 'compute', not both")
        if self.source is None and self.compute is None and self.default is _MISSING:
            raise ValueError("needs 'from', 'compute' or 'default'")
        return self


@dataclass(frozen=True)
class RecordError:
    index: int
    field: str
    message: str

    def __str__(self) -> str:
        return f"record {self.index}, field '{self.field}': {self.message}"


@dataclass
class MapResult:
    records: list[Record] = field(default_factory=list)
    errors: list[RecordError] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)  # indexes that failed

    @property
    def ok(self) -> bool:
        return not self.errors

    def report(self) -> str:
        head = f"{len(self.records)} mapped, {len(self.skipped)} rejected"
        return "\n".join([head, *(f"  - {e}" for e in self.errors)])


@dataclass(frozen=True)
class Change:
    """One record in a dry run: what writing it would do."""

    index: int
    action: Literal["create", "update", "unchanged"]
    key: Any
    diff: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # field -> (old, new)


@dataclass
class DryRun:
    changes: list[Change]
    errors: list[RecordError]

    def counts(self) -> dict[str, int]:
        out = {"create": 0, "update": 0, "unchanged": 0, "rejected": len({e.index for e in self.errors})}
        for change in self.changes:
            out[change.action] += 1
        return out

    def report(self) -> str:
        c = self.counts()
        lines = [
            f"would create {c['create']}, update {c['update']}, leave {c['unchanged']}, "
            + f"reject {c['rejected']}"
        ]
        for ch in self.changes:
            if ch.action == "create":
                lines.append(f"  + {ch.key}")
            elif ch.action == "update":
                lines.append(f"  ~ {ch.key}: " + ", ".join(
                    f"{k}: {old!r} -> {new!r}" for k, (old, new) in ch.diff.items()))
        lines += [f"  ! {e}" for e in self.errors]
        return "\n".join(lines)


class Mapper:
    """Applies a mapping to records. Build with :func:`load_mapping` or a dict."""

    def __init__(self, spec: MappingABC[str, Any], *, validate: type[BaseModel] | None = None):
        if not isinstance(spec, MappingABC) or not isinstance(spec.get("fields"), MappingABC):
            raise MappingError("mapping needs a 'fields:' section")
        if not spec["fields"]:
            raise MappingError("mapping 'fields:' is empty")
        unknown = set(spec) - {"name", "fields"}
        if unknown:
            raise MappingError(f"unknown top-level keys: {sorted(unknown)}")
        self.name: str = str(spec.get("name", "mapping"))
        self.fields: dict[str, FieldSpec] = {}
        for target, raw in spec["fields"].items():
            try:
                self.fields[str(target)] = FieldSpec.model_validate(raw)
            except ValidationError as err:
                reason = "; ".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}" if e["loc"] else e["msg"]
                    for e in err.errors()
                )
                raise MappingError(f"field '{target}': {reason}") from None
        self.validate = validate

    # -- one record ---------------------------------------------------------
    def _build(self, spec: FieldSpec, row: MappingABC[str, Any]) -> Any:
        if spec.compute is not None:
            def fill(match: re.Match[str]) -> str:
                key = match.group(1).strip()
                if key not in row:
                    raise KeyError(key)
                return "" if row[key] is None else str(row[key])
            try:
                value: Any = _TEMPLATE_RE.sub(fill, spec.compute)
            except KeyError as err:
                raise ValueError(f"source column {err.args[0]!r} not found") from None
        elif spec.source is not None:
            if spec.source not in row:
                if spec.default is _MISSING and spec.required:
                    raise ValueError(f"source column {spec.source!r} not found")
                value = _MISSING
            else:
                value = row[spec.source]
        else:
            value = _MISSING
        if isinstance(value, str):
            for name in spec.transform:
                value = TRANSFORMS[name](value)
        if value is _MISSING or value is None or (isinstance(value, str) and value == ""):
            if spec.default is not _MISSING:
                return spec.default
            if spec.required:
                raise ValueError("required value is missing or empty")
            return None
        if spec.cast:
            try:
                value = CASTS[spec.cast](value)
            except (ValueError, TypeError):
                raise ValueError(f"cannot cast {value!r} to {spec.cast}") from None
        return value

    def map_record(self, row: MappingABC[str, Any], index: int = 0) -> tuple[Record | None, list[RecordError]]:
        """Map one record; returns ``(record, [])`` or ``(None, errors)``."""
        out: Record = {}
        errors: list[RecordError] = []
        for target, spec in self.fields.items():
            try:
                out[target] = self._build(spec, row)
            except ValueError as err:
                errors.append(RecordError(index, target, str(err)))
        if not errors and self.validate is not None:
            try:
                out = self.validate.model_validate(out).model_dump()
            except ValidationError as err:
                errors += [RecordError(index, ".".join(map(str, e["loc"])) or "<record>", e["msg"])
                           for e in err.errors()]
        return (None, errors) if errors else (out, [])

    # -- batches ------------------------------------------------------------
    def apply(self, rows: Iterable[MappingABC[str, Any]]) -> MapResult:
        result = MapResult()
        for i, row in enumerate(rows):
            rec, errs = self.map_record(row, i)
            if rec is None:
                result.errors += errs
                result.skipped.append(i)
            else:
                result.records.append(rec)
        return result

    def dry_run(self, rows: Iterable[MappingABC[str, Any]], *, key: str,
                existing: Iterable[MappingABC[str, Any]] = ()) -> DryRun:
        """Preview a write: create / update (with field diffs) / unchanged, nothing sent."""
        if key not in self.fields:
            raise MappingError(f"key '{key}' is not a mapped field")
        current = {r.get(key): r for r in existing}
        result = self.apply(rows)
        changes: list[Change] = []
        seen: dict[Any, int] = {}
        good = iter(result.records)
        bad = set(result.skipped)
        for i in range(len(result.records) + len(bad)):
            if i in bad:
                continue
            rec = next(good)
            k = rec.get(key)
            if k in seen:
                result.errors.append(RecordError(i, key, f"duplicate key {k!r} (also record {seen[k]})"))
                continue
            seen[k] = i
            old = current.get(k)
            if old is None:
                changes.append(Change(i, "create", k))
                continue
            diff = {f: (old.get(f), v) for f, v in rec.items() if old.get(f) != v}
            changes.append(Change(i, "update" if diff else "unchanged", k, diff))
        return DryRun(changes, sorted(result.errors, key=lambda e: e.index))


def load_mapping(source: str | Path, *, validate: type[BaseModel] | None = None) -> Mapper:
    """Load a mapping from a YAML file path or a YAML string."""
    text = str(source)
    if isinstance(source, Path) or ("\n" not in text and text.endswith((".yml", ".yaml"))):
        try:
            text = Path(source).read_text(encoding="utf-8")
        except OSError as err:
            raise MappingError(f"cannot read mapping file: {err}") from None
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise MappingError(f"invalid YAML: {err}") from None
    return Mapper(spec, validate=validate)
