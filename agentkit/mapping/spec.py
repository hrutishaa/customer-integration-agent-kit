"""Declarative mapping specs, validated with Pydantic before any row is touched."""
from __future__ import annotations

import keyword
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentkit.mapping.cast import cast_value, is_blank
from agentkit.mapping.errors import CastError, ExprError
from agentkit.mapping.expr import FUNCTIONS, SafeExpr

CastName = Literal["str", "int", "float", "bool", "date", "datetime", "decimal"]
RESERVED_FIELDS = frozenset(FUNCTIONS) | {"row"}


class FieldSpec(BaseModel):
    """One output field: rename (``from``), ``cast``, ``default``, or ``compute``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str | None = Field(default=None, alias="from")
    cast: CastName | None = None
    default: Any = None
    compute: str | None = None
    required: bool = False

    @model_validator(mode="after")
    def _check(self) -> FieldSpec:
        if self.source is not None and self.compute is not None:
            raise ValueError("set only one of 'from' and 'compute'")
        if self.source is None and self.compute is None and not self.has_default():
            raise ValueError("field needs 'from', 'compute', or 'default'")
        if self.source is not None and (
            not self.source.strip() or any(part == "" for part in self.source.split("."))
        ):
            raise ValueError(f"invalid field path {self.source!r}")
        if self.compute is not None:
            try:
                SafeExpr(self.compute)
            except ExprError as exc:
                raise ValueError(str(exc)) from exc
        if self.has_default() and self.cast and not is_blank(self.default):
            try:
                cast_value(self.default, self.cast)
            except CastError as exc:
                raise ValueError(f"default is not a valid {self.cast}: {exc}") from exc
        if (
            self.has_default()
            and self.cast
            and self.cast != "str"
            and isinstance(self.default, str)
            and self.default.strip() == ""
        ):
            raise ValueError(f"default '' is not a valid {self.cast}")
        return self

    def has_default(self) -> bool:
        """True when the YAML/Python spec set ``default``, even to null."""
        return "default" in self.model_fields_set


class MappingSpec(BaseModel):
    """A named target schema. ``strict`` drops source keys that were not mapped."""

    model_config = ConfigDict(extra="forbid")

    name: str
    fields: dict[str, FieldSpec]
    strict: bool = True
    on_error: Literal["collect", "raise"] = "collect"
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
            return data
        converted: dict[str, Any] = {}
        for index, item in enumerate(data["fields"]):
            try:
                item = dict(item)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"fields[{index}] must be a mapping") from exc
            name = item.pop("name", None)
            target = item.pop("target", None)
            if isinstance(name, str) and isinstance(target, str) and name != target:
                raise ValueError(f"fields[{index}] has both name and target")
            chosen = name if isinstance(name, str) else target
            if not isinstance(chosen, str) or not chosen:
                raise ValueError(f"fields[{index}] needs a name")
            if chosen in converted:
                raise ValueError(f"duplicate field {chosen!r}")
            converted[chosen] = item
        return {**data, "fields": converted}

    @model_validator(mode="after")
    def _check_names(self) -> MappingSpec:
        if not self.name.strip():
            raise ValueError("mapping name cannot be empty")
        if not self.fields:
            raise ValueError("mapping needs at least one field")
        for name in self.fields:
            if not name.isidentifier() or keyword.iskeyword(name) or name in RESERVED_FIELDS:
                raise ValueError(f"target field {name!r} is not a usable name")
            if name.startswith("model_"):
                raise ValueError(f"target field {name!r} is reserved")
        return self


__all__ = ["FieldSpec", "MappingSpec"]
