"""Restricted compute expressions for field mappings.

Customer mappings need ``lower(email)`` and ``concat(first, ' ', last)``. They
do not need arbitrary Python: there is no attribute access on objects, no
imports, and no dunder names. Names resolve to source fields, target fields
already produced, or ``row['Column Name']`` for headers that are not identifiers.
"""
from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

from agentkit.mapping.cast import cast_value
from agentkit.mapping.errors import CastError, ExprError

_MAX_EXPR_LEN = 500
_MAX_NODES = 100
_MAX_EXPONENT = 32
_MAX_REPEAT = 10_000

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_CMPS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_ALLOWED = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.IfExp,
    ast.Compare,
    ast.Call,
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)


def _require_str(value: Any, fn: str) -> str:
    if not isinstance(value, str):
        raise ExprError(f"{fn}() expects a string")
    return value


def _lower(value: Any) -> str:
    return _require_str(value, "lower").lower()


def _upper(value: Any) -> str:
    return _require_str(value, "upper").upper()


def _strip(value: Any) -> str:
    return _require_str(value, "strip").strip()


def _title(value: Any) -> str:
    return _require_str(value, "title").title()


def _as_int(value: Any) -> int:
    try:
        casted = cast_value(value, "int")
    except CastError as exc:
        raise ExprError(str(exc)) from exc
    if casted is None:
        raise ExprError("int() does not accept null")
    return casted


def _as_float(value: Any) -> float:
    try:
        casted = cast_value(value, "float")
    except CastError as exc:
        raise ExprError(str(exc)) from exc
    if casted is None:
        raise ExprError("float() does not accept null")
    return casted


def _as_bool(value: Any) -> bool:
    try:
        casted = cast_value(value, "bool")
    except CastError as exc:
        raise ExprError(str(exc)) from exc
    if casted is None:
        raise ExprError("bool() does not accept null")
    return casted


def _as_str(value: Any) -> str:
    if value is None:
        raise ExprError("str() does not accept null")
    try:
        return cast_value(value, "str")
    except CastError as exc:
        raise ExprError(str(exc)) from exc


def _length(value: Any) -> int:
    if value is None:
        raise ExprError("len() does not accept null")
    try:
        return len(value)
    except TypeError as exc:
        raise ExprError("len() expects a string or collection") from exc


def _absolute(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExprError("abs() expects a number")
    return abs(value)


def _rounded(value: Any, ndigits: Any = 0) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExprError("round() expects a number")
    if isinstance(ndigits, bool) or not isinstance(ndigits, int):
        raise ExprError("round() digits must be an integer")
    return round(value, ndigits)


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is None or (isinstance(value, str) and value.strip() == ""):
            continue
        return value
    return None


def _concat(*values: Any) -> str:
    return "".join("" if value is None else str(value) for value in values)


def _replace(value: Any, old: Any, new: Any) -> str:
    text = _require_str(value, "replace")
    return text.replace(_require_str(old, "replace"), _require_str(new, "replace"))


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "lower": _lower,
    "upper": _upper,
    "strip": _strip,
    "title": _title,
    "len": _length,
    "abs": _absolute,
    "round": _rounded,
    "str": _as_str,
    "int": _as_int,
    "float": _as_float,
    "bool": _as_bool,
    "coalesce": _coalesce,
    "concat": _concat,
    "replace": _replace,
}


class SafeExpr:
    """A compiled compute expression. Constructing it rejects unsafe syntax."""

    def __init__(self, source: str) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ExprError("compute expression cannot be empty")
        if len(source) > _MAX_EXPR_LEN:
            raise ExprError("compute expression is too long")
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExprError(f"invalid compute expression: {source!r}") from exc
        count = sum(1 for _ in ast.walk(tree))
        if count > _MAX_NODES:
            raise ExprError("compute expression is too complex")
        self._validate(tree)
        self._tree = tree
        self.source = source

    def __call__(self, env: dict[str, Any]) -> Any:
        try:
            return self._eval(self._tree.body, env)
        except ExprError:
            raise
        except ZeroDivisionError as exc:
            raise ExprError("division by zero") from exc
        except (TypeError, ValueError, KeyError, IndexError, ArithmeticError) as exc:
            raise ExprError(f"compute failed: {exc}") from exc

    def referenced_keys(self) -> set[str]:
        """Source keys this expression reads (not function names, not ``row``)."""
        keys: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                keys.add(node.id)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "row"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                keys.add(node.slice.value)
        keys -= calls
        keys.discard("row")
        keys |= _row_roots(self._tree)
        return keys

    def _validate(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED):
                raise ExprError(f"unsupported syntax in compute: {type(node).__name__}")
            if isinstance(node, ast.Name) and node.id.startswith("_"):
                raise ExprError("private names are not allowed in compute")
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                raise ExprError("private attribute access is not allowed")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    raise ExprError("method calls are not allowed in compute")
                if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                    called = node.func
                    name = called.id if isinstance(called, ast.Name) else type(called).__name__
                    raise ExprError(f"unknown function {name!r}")
                if node.keywords:
                    raise ExprError("keyword arguments are not supported in compute")
            if isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, float, bool, type(None))):
                raise ExprError("unsupported literal in compute")

    def _eval(self, node: ast.AST, env: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise ExprError(f"unknown field {node.id!r}")
            return env[node.id]
        if isinstance(node, ast.Attribute):
            return self._lookup_attr(self._eval(node.value, env), node.attr)
        if isinstance(node, ast.Subscript):
            return self._lookup_item(self._eval(node.value, env), self._eval(node.slice, env))
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](self._eval(node.operand, env))
        if isinstance(node, ast.BinOp):
            return self._binop(node, env)
        if isinstance(node, ast.BoolOp):
            return self._boolop(node, env)
        if isinstance(node, ast.Compare):
            return self._compare(node, env)
        if isinstance(node, ast.IfExp):
            branch = node.body if self._eval(node.test, env) else node.orelse
            return self._eval(branch, env)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExprError("unknown function")
            args = [self._eval(arg, env) for arg in node.args]
            return FUNCTIONS[node.func.id](*args)
        raise ExprError(f"unsupported syntax in compute: {type(node).__name__}")

    def _binop(self, node: ast.BinOp, env: dict[str, Any]) -> Any:
        left = self._eval(node.left, env)
        right = self._eval(node.right, env)
        if isinstance(node.op, ast.Pow):
            return _power(left, right)
        if isinstance(node.op, ast.Mult):
            _guard_repeat(left, right)
        return _BINOPS[type(node.op)](left, right)

    def _boolop(self, node: ast.BoolOp, env: dict[str, Any]) -> Any:
        if isinstance(node.op, ast.And):
            value: Any = True
            for part in node.values:
                value = self._eval(part, env)
                if not value:
                    return value
            return value
        value = False
        for part in node.values:
            value = self._eval(part, env)
            if value:
                return value
        return value

    def _compare(self, node: ast.Compare, env: dict[str, Any]) -> bool:
        left = self._eval(node.left, env)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self._eval(comparator, env)
            try:
                ok = _CMPS[type(op)](left, right)
            except TypeError as exc:
                raise ExprError(f"cannot compare {left!r} and {right!r}") from exc
            if not ok:
                return False
            left = right
        return True

    @staticmethod
    def _lookup_attr(value: Any, attr: str) -> Any:
        if attr.startswith("_"):
            raise ExprError("private attribute access is not allowed")
        if isinstance(value, dict):
            if attr not in value:
                raise ExprError(f"unknown field {attr!r}")
            return value[attr]
        raise ExprError(f"cannot read .{attr} on a {type(value).__name__}")

    @staticmethod
    def _lookup_item(value: Any, key: Any) -> Any:
        if isinstance(value, dict):
            if not isinstance(key, str) or key not in value:
                raise ExprError(f"unknown field {key!r}")
            return value[key]
        if isinstance(value, (list, tuple, str)):
            if not isinstance(key, int) or isinstance(key, bool):
                raise ExprError("index must be an integer")
            return value[key]
        raise ExprError(f"cannot index a {type(value).__name__}")


def _row_roots(tree: ast.AST) -> set[str]:
    """Top-level keys read through ``row.field`` or ``row['Field']``."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        root = _root_key(node)
        if root is not None:
            roots.add(root)
    return roots


def _root_key(node: ast.AST) -> str | None:
    steps: list[str] = []
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            steps.append(current.attr)
            current = current.value
            continue
        if isinstance(current, ast.Subscript) and isinstance(current.slice, ast.Constant):
            if isinstance(current.slice.value, str):
                steps.append(str(current.slice.value))
            current = current.value
            continue
        break
    if isinstance(current, ast.Name) and current.id == "row" and steps:
        return steps[-1]
    return None


def _power(left: Any, right: Any) -> Any:
    if isinstance(left, bool) or isinstance(right, bool):
        raise ExprError("exponent does not accept booleans")
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        raise ExprError("exponent expects numbers")
    if abs(right) > _MAX_EXPONENT:
        raise ExprError("exponent too large")
    return left**right


def _guard_repeat(left: Any, right: Any) -> None:
    text, count = (left, right) if isinstance(left, str) else (right, left)
    if not isinstance(text, str):
        return
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > _MAX_REPEAT:
        raise ExprError("string repeat is out of range")
