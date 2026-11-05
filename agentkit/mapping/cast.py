"""Type casts for mapped fields.

Blank strings are not values for non-string casts: they become ``None`` so a
default can fill them. Booleans are never treated as integers.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from agentkit.mapping.errors import CastError

CASTS = ("str", "int", "float", "bool", "date", "datetime", "decimal")

_TRUE = frozenset({"true", "yes", "y", "1", "t"})
_FALSE = frozenset({"false", "no", "n", "0", "f"})
_INT_RE = re.compile(r"[+-]?\d+")


def is_blank(value: Any) -> bool:
    """True for ``None`` and whitespace-only strings (missing customer data)."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def cast_value(value: Any, kind: str) -> Any:
    """Cast ``value`` to ``kind``. ``None`` and blank strings become ``None``."""
    if kind not in _CASTERS:
        raise CastError(f"unknown cast {kind!r}")
    if value is None or (kind != "str" and isinstance(value, str) and value.strip() == ""):
        return None
    try:
        return _CASTERS[kind](value)
    except CastError:
        raise
    except (TypeError, ValueError, ArithmeticError, InvalidOperation) as exc:
        raise CastError(f"cannot cast {value!r} to {kind}") from exc


def _fail(value: Any, kind: str) -> Any:
    raise CastError(f"cannot cast {value!r} to {kind}")


def _cast_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        _fail(value, "str")
    return str(value)


def _cast_int(value: Any) -> int:
    if isinstance(value, bool):
        _fail(value, "int")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            _fail(value, "int")
        return int(value)
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            _fail(value, "int")
        return int(value)
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    _fail(value, "int")
    return 0


def _cast_float(value: Any) -> float:
    if isinstance(value, bool):
        _fail(value, "float")
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
    elif isinstance(value, str):
        number = float(value.strip())
    else:
        _fail(value, "float")
        return 0.0
    if not math.isfinite(number):
        _fail(value, "float")
    return number


def _cast_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    _fail(value, "bool")
    return False


def _normalize_iso(text: str) -> str:
    text = text.strip()
    if text.endswith(("Z", "z")):
        return text[:-1] + "+00:00"
    return text


def _cast_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = _normalize_iso(value)
        parsed = datetime.fromisoformat(text) if "T" in text or " " in text else date.fromisoformat(text)
        return parsed.date() if isinstance(parsed, datetime) else parsed
    _fail(value, "date")
    return date.min


def _cast_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        return datetime.fromisoformat(_normalize_iso(value))
    _fail(value, "datetime")
    return datetime.min


def _cast_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        _fail(value, "decimal")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            _fail(value, "decimal")
        number = Decimal(str(value))
    elif isinstance(value, str):
        number = Decimal(value.strip())
    else:
        _fail(value, "decimal")
        return Decimal(0)
    if not number.is_finite():
        _fail(value, "decimal")
    return number


_CASTERS = {
    "str": _cast_str,
    "int": _cast_int,
    "float": _cast_float,
    "bool": _cast_bool,
    "date": _cast_date,
    "datetime": _cast_datetime,
    "decimal": _cast_decimal,
}
