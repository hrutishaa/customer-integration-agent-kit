"""Apply a declarative mapping to records, with a per-row error report and a diff.

A bad cast or a missing required field fails that row only. ``apply`` returns
the valid rows plus a :class:`RecordError` for each rejected one. ``dry_run``
returns the same decisions as a field-level diff and never raises for bad data.
"""
from __future__ import annotations

import keyword
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from agentkit.connectors.base import Record
from agentkit.mapping.cast import cast_value, is_blank
from agentkit.mapping.errors import (
    CastError,
    ExprError,
    FieldError,
    MappingError,
    RecordError,
    failure_message,
)
from agentkit.mapping.expr import SafeExpr
from agentkit.mapping.spec import FieldSpec, MappingSpec

_CAST_TYPES: dict[str, Any] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "date": date,
    "datetime": datetime,
    "decimal": Decimal,
}


@dataclass(frozen=True)
class FieldChange:
    """One target field in a dry-run diff. ``op`` is how the value was produced."""

    field: str
    op: str  # rename | cast | default | compute | copy
    before: Any
    after: Any
    source: str | None = None

    @property
    def changed(self) -> bool:
        return self.op != "copy"


@dataclass(frozen=True)
class RecordDiff:
    """Dry-run result for one source row. ``mapped`` is ``None`` when it failed."""

    index: int
    changes: tuple[FieldChange, ...]
    dropped: tuple[str, ...]
    errors: tuple[FieldError, ...]
    mapped: Record | None


@dataclass
class MappingReport:
    """Valid rows from :meth:`SchemaMapper.apply`, plus one error per bad row."""

    records: list[Record]
    errors: list[RecordError]
    accepted_indexes: list[int]

    @property
    def ok(self) -> int:
        return len(self.records)

    @property
    def failed(self) -> int:
        return len(self.errors)

    def raise_for_errors(self) -> MappingReport:
        """Raise :class:`MappingError` when any row failed. Returns ``self``."""
        if self.errors:
            raise MappingError(failure_message(self.errors), errors=self.errors, report=self)
        return self


@dataclass
class DiffReport:
    """Preview of a mapping. One :class:`RecordDiff` per input row, in order."""

    diffs: list[RecordDiff]

    @property
    def ok(self) -> int:
        return sum(1 for diff in self.diffs if not diff.errors)

    @property
    def failed(self) -> int:
        return sum(1 for diff in self.diffs if diff.errors)

    def summary(self) -> dict[str, int]:
        counts = {"rename": 0, "cast": 0, "default": 0, "compute": 0}
        dropped = 0
        for diff in self.diffs:
            dropped += len(diff.dropped)
            for change in diff.changes:
                if change.op in counts:
                    counts[change.op] += 1
        return {
            "records": len(self.diffs),
            "ok": self.ok,
            "failed": self.failed,
            "dropped": dropped,
            **counts,
        }

    def render(self) -> str:
        """Human-readable diff. Copy-only fields are omitted; errors are not."""
        lines: list[str] = []
        changed = 0
        for diff in self.diffs:
            status = "ok" if not diff.errors else "ERROR"
            lines.append(f"record {diff.index}  {status}")
            for change in diff.changes:
                if change.op == "copy":
                    continue
                changed += 1
                origin = f"  (from {change.source})" if change.source else ""
                lines.append(
                    f"  {change.op:8} {change.field}: {change.before!r} -> {change.after!r}{origin}"
                )
            for err in diff.errors:
                lines.append(f"  {'error':8} {err.field}: {err.message}")
            if diff.dropped:
                lines.append(f"  {'dropped':8} {', '.join(diff.dropped)}")
        lines.append(f"{self.ok} ok, {self.failed} failed, {changed} field change(s)")
        return "\n".join(lines)


@dataclass
class _Outcome:
    index: int
    mapped: Record | None
    changes: tuple[FieldChange, ...]
    dropped: tuple[str, ...]
    errors: tuple[FieldError, ...]
    source: Record | None


class SchemaMapper:
    """Map source records onto a target schema.

    Load a spec with :meth:`from_yaml` or :meth:`from_file`, or pass a
    :class:`MappingSpec` built in Python. Specs are checked up front (unknown
    keys, bad casts, unsafe compute). Rows are checked one by one.

    Compute expressions may use literals, arithmetic, comparisons, ``and`` /
    ``or`` / ``not``, and ``a if cond else b``. Names are source fields and
    target fields produced earlier in the spec. ``row['E-mail']`` reads a
    column that is not an identifier. Functions: ``lower``, ``upper``,
    ``strip``, ``title``, ``len``, ``abs``, ``round``, ``str``, ``int``,
    ``float``, ``bool``, ``coalesce``, ``concat``, ``replace``.

    Blank strings and nulls are missing, so ``default`` fills them. YAML
    ``yes`` / ``no`` / unquoted dates are parsed by YAML itself; quote them
    when they must stay strings. Expressions cannot import, call methods, or
    touch private names.
    """

    def __init__(self, spec: MappingSpec) -> None:
        if not isinstance(spec, MappingSpec):
            raise MappingError("pass a MappingSpec, or use from_yaml / from_file")
        self.spec = spec
        self._exprs: dict[str, SafeExpr] = {}
        for name, field_spec in spec.fields.items():
            if field_spec.compute is not None:
                self._exprs[name] = SafeExpr(field_spec.compute)
        self._consumed = _consumed_keys(spec, self._exprs)
        self._model = _record_model(spec)

    @classmethod
    def from_yaml(cls, text: str) -> SchemaMapper:
        """Load a mapping from a YAML string (``yaml.safe_load``)."""
        if not isinstance(text, str):
            raise MappingError("from_yaml expects a YAML string; use from_file for a path")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise MappingError(f"invalid mapping YAML: {exc}") from exc
        return cls._from_data(data)

    @classmethod
    def from_file(cls, path: str | Path) -> SchemaMapper:
        """Load a mapping document from ``path``."""
        file = Path(path)
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise MappingError(f"cannot read mapping {file}: {exc}") from exc
        return cls.from_yaml(text)

    @classmethod
    def _from_data(cls, data: Any) -> SchemaMapper:
        if not isinstance(data, dict):
            raise MappingError("mapping YAML must be a mapping")
        try:
            spec = MappingSpec.model_validate(data)
        except ValidationError as exc:
            raise MappingError(_format_validation(exc)) from exc
        return cls(spec)

    def map_one(self, record: Record) -> Record:
        """Map a single record or raise :class:`MappingError`."""
        return self.apply([record], on_error="raise").records[0]

    def apply(
        self,
        records: Iterable[Record],
        *,
        on_error: Literal["collect", "raise"] | None = None,
    ) -> MappingReport:
        """Map a batch. Bad rows are reported instead of aborting the batch.

        ``on_error='raise'`` still inspects the whole batch, then raises with
        every problem attached to the exception.
        """
        mode = on_error or self.spec.on_error
        if mode not in ("collect", "raise"):
            raise MappingError("on_error must be 'collect' or 'raise'")
        kept: list[Record] = []
        indexes: list[int] = []
        errors: list[RecordError] = []
        for outcome in self._transform(records):
            if outcome.errors:
                errors.append(RecordError(outcome.index, outcome.errors, outcome.source))
            elif outcome.mapped is not None:
                kept.append(outcome.mapped)
                indexes.append(outcome.index)
        report = MappingReport(kept, errors, indexes)
        if mode == "raise" and errors:
            raise MappingError(failure_message(errors), errors=errors, report=report)
        return report

    def dry_run(self, records: Iterable[Record]) -> DiffReport:
        """Preview every row as a diff. Never raises because a row is invalid."""
        diffs = [
            RecordDiff(item.index, item.changes, item.dropped, item.errors, item.mapped)
            for item in self._transform(records)
        ]
        return DiffReport(diffs)

    def _transform(self, records: Iterable[Record]) -> Iterator[_Outcome]:
        for index, raw in enumerate(records):
            yield self._one(index, raw)

    def _one(self, index: int, raw: Any) -> _Outcome:
        try:
            source = dict(raw)
        except (TypeError, ValueError):
            err = FieldError("__record__", "record must be a mapping", raw, "record")
            return _Outcome(index, None, (), (), (err,), None)

        output: dict[str, Any] = {}
        changes: list[FieldChange] = []
        errors: list[FieldError] = []
        failed: set[str] = set()
        env: dict[str, Any] = {"row": source}
        for key, value in source.items():
            if key.isidentifier() and not _hidden_name(key):
                env[key] = value

        for name, field_spec in self.spec.fields.items():
            if field_spec.compute is not None and (self._exprs[name].referenced_keys() & failed):
                continue
            try:
                value, change = _produce(name, field_spec, source, env, self._exprs.get(name))
            except CastError as exc:
                errors.append(FieldError(name, str(exc), _raw_for_error(field_spec, source), "cast"))
                failed.add(name)
                continue
            except ExprError as exc:
                errors.append(FieldError(name, str(exc), None, "compute"))
                failed.add(name)
                continue
            if _required_missing(field_spec, value):
                errors.append(FieldError(name, "required field is missing", change.before, "required"))
                failed.add(name)
                continue
            output[name] = value
            env[name] = value
            changes.append(change)

        dropped = tuple(key for key in source if key not in self._consumed) if self.spec.strict else ()
        if errors:
            return _Outcome(index, None, tuple(changes), dropped, tuple(errors), dict(source))

        if not self.spec.strict:
            output = {key: value for key, value in source.items() if key not in self._consumed} | output
        try:
            validated = self._model.model_validate(output).model_dump()
        except ValidationError as exc:
            field_errors = tuple(_pydantic_errors(exc))
            return _Outcome(index, None, tuple(changes), dropped, field_errors, dict(source))
        return _Outcome(index, validated, tuple(changes), dropped, (), dict(source))


def _hidden_name(name: str) -> bool:
    return keyword.iskeyword(name) or name == "row" or name.startswith("_")


def _consumed_keys(spec: MappingSpec, exprs: dict[str, SafeExpr]) -> set[str]:
    used: set[str] = set()
    for field_spec in spec.fields.values():
        if field_spec.source:
            used.add(field_spec.source.split(".", 1)[0])
    for expr in exprs.values():
        used |= expr.referenced_keys()
    return used


def _produce(
    name: str,
    field_spec: FieldSpec,
    source: Record,
    env: dict[str, Any],
    expr: SafeExpr | None,
) -> tuple[Any, FieldChange]:
    if expr is not None:
        value = expr(env)
        before: Any = None
        op = "compute"
        origin: str | None = None
        if field_spec.cast and not is_blank(value):
            casted = cast_value(value, field_spec.cast)
            value = casted
        elif field_spec.cast and is_blank(value):
            value = None
        if is_blank(value) and field_spec.has_default():
            value = _default_value(field_spec)
            op = "default"
        return value, FieldChange(name, op, before, value, origin)

    found_value, found = _lookup(source, field_spec.source or name)
    before = found_value if found else None
    if not found or is_blank(found_value):
        if field_spec.has_default():
            value = _default_value(field_spec)
            return value, FieldChange(name, "default", before, value, field_spec.source)
        value = None
        op = "rename" if field_spec.source not in (None, name) else "copy"
        return value, FieldChange(name, op, before, None, field_spec.source)

    value = found_value
    op = "copy"
    if field_spec.cast:
        casted = cast_value(value, field_spec.cast)
        if casted != value or type(casted) is not type(value):
            op = "cast"
        value = casted
    if op == "copy" and field_spec.source not in (None, name):
        op = "rename"
    return value, FieldChange(name, op, before, value, field_spec.source)


def _default_value(field_spec: FieldSpec) -> Any:
    value = field_spec.default
    if field_spec.cast and not is_blank(value):
        return cast_value(value, field_spec.cast)
    return value


def _required_missing(field_spec: FieldSpec, value: Any) -> bool:
    return field_spec.required and is_blank(value)


def _lookup(record: Record, path: str) -> tuple[Any, bool]:
    current: Any = record
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        return None, False
    return current, True


def _raw_for_error(field_spec: FieldSpec, source: Record) -> Any:
    if field_spec.source is None:
        return None
    value, found = _lookup(source, field_spec.source)
    return value if found else None


def _record_model(spec: MappingSpec) -> type[BaseModel]:
    definitions: dict[str, Any] = {}
    for name, field_spec in spec.fields.items():
        py_type = _CAST_TYPES.get(field_spec.cast or "", Any)
        annotation = py_type if field_spec.required else py_type | None
        definitions[name] = (annotation, ... if field_spec.required else None)
    config = ConfigDict(extra="forbid" if spec.strict else "allow", strict=True)

    class _Base(BaseModel):
        model_config = config

    return create_model(_model_name(spec.name), __base__=_Base, **definitions)


def _model_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in name)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "M_" + cleaned
    return cleaned


def _pydantic_errors(exc: ValidationError) -> list[FieldError]:
    errors: list[FieldError] = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        field_name = str(loc[0]) if loc else "__record__"
        errors.append(FieldError(field_name, str(err.get("msg", "invalid")), err.get("input"), "validation"))
    return errors


def _format_validation(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        message = str(err.get("msg", "invalid"))
        parts.append(f"{loc}: {message}" if loc else message)
    return "invalid mapping: " + "; ".join(parts)
