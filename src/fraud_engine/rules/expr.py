"""A small, safe expression language for rule conditions.

    card_failed_attempts_1h >= 5 and payment_approved
    merchant_category in ("gift_cards", "crypto") and not category_first_time

Conditions are parsed with Python's `ast` module, every node is checked against an allow-list
(comparisons, and/or/not, feature names, literals), and the tree is compiled into plain
closures. Nothing is passed to eval() or compile(), so a rules file cannot run code.

A comparison with a missing value (None) is False, except `==`, `!=`, `is` and `is not`.
So `amount_zscore > 5` simply doesn't fire for a user with no spending history yet.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Collection, Mapping
from typing import Any

Features = Mapping[str, Any]
Expr = Callable[[Features], Any]


class ConditionError(ValueError):
    pass


# YAML authors tend to write true/false/null; accept them alongside Python's spelling.
_LITERAL_NAMES = {"true": True, "false": False, "null": None}
_CONSTANT_TYPES = (bool, int, float, str, type(None))

_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
_NONE_SAFE = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)


def compile_condition(source: str, known: Collection[str]) -> tuple[Expr, frozenset[str]]:
    """Compile a condition; returns the predicate and the feature names it reads."""
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"invalid syntax in {source!r}: {exc.msg}") from None
    names: set[str] = set()
    expr = _compile(tree.body, known, names)
    return (lambda f: bool(expr(f))), frozenset(names)


def _constant(value: Any) -> Expr:
    return lambda _f: value


def _compile(node: ast.AST, known: Collection[str], names: set[str]) -> Expr:
    match node:
        case ast.BoolOp(op=ast.And(), values=values):
            parts = [_compile(v, known, names) for v in values]
            return lambda f: all(p(f) for p in parts)
        case ast.BoolOp(op=ast.Or(), values=values):
            parts = [_compile(v, known, names) for v in values]
            return lambda f: any(p(f) for p in parts)
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            inner = _compile(operand, known, names)
            return lambda f: not inner(f)
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=v)) if isinstance(
            v, int | float
        ) and not isinstance(v, bool):
            return _constant(-v)
        case ast.Compare(left=left, ops=ops, comparators=comparators):
            return _compile_compare(left, ops, comparators, known, names)
        case ast.Name(id=name) if name in _LITERAL_NAMES:
            return _constant(_LITERAL_NAMES[name])
        case ast.Name(id=name):
            if name not in known:
                raise ConditionError(f"unknown feature {name!r}")
            names.add(name)
            return lambda f: f.get(name)
        case ast.Constant(value=value) if isinstance(value, _CONSTANT_TYPES):
            return _constant(value)
        case ast.Tuple(elts=elts) | ast.List(elts=elts) | ast.Set(elts=elts):
            values = []
            for elt in elts:
                if not (isinstance(elt, ast.Constant) and isinstance(elt.value, _CONSTANT_TYPES)):
                    raise ConditionError("lists in a condition may only contain literals")
                values.append(elt.value)
            return _constant(frozenset(values))
    raise ConditionError(f"{ast.unparse(node)!r} is not allowed in a condition")


def _compile_compare(
    left: ast.AST,
    ops: list[ast.cmpop],
    comparators: list[ast.expr],
    known: Collection[str],
    names: set[str],
) -> Expr:
    operands = [_compile(n, known, names) for n in (left, *comparators)]
    steps = []
    for i, op in enumerate(ops):
        fn = _COMPARE.get(type(op))
        if fn is None:
            raise ConditionError(f"operator {type(op).__name__} is not allowed")
        steps.append((fn, isinstance(op, _NONE_SAFE), operands[i], operands[i + 1]))

    def compare(f: Features) -> bool:
        # Chained like Python: a < b < c means a < b and b < c.
        for fn, none_safe, lhs, rhs in steps:
            a, b = lhs(f), rhs(f)
            if not none_safe and (a is None or b is None):
                return False
            try:
                if not fn(a, b):
                    return False
            except TypeError:  # e.g. comparing a string feature with a number
                return False
        return True

    return compare
