"""Value parsing and comparison shared by discovery, replay and conditions.

One implementation, so the parser that *validates* an extracted value during
discovery is exactly the parser replay uses to produce the output.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .schema.artifact import ValueType


class ParseError(ValueError):
    pass


_NUM = re.compile(r"-?[\d,]*\.?\d+")


def parse_value(raw: str, vtype: ValueType, pattern: str | None = None) -> Any:
    """Raw displayed text -> typed output. Raises ParseError (never guesses)."""
    s = raw
    if pattern:
        m = re.search(pattern, raw)
        if not m:
            raise ParseError(f"text does not match {pattern}")
        s = m.group(1) if m.groups() else m.group(0)
    s = s.strip()
    if vtype in (ValueType.decimal, ValueType.integer):
        neg = s.startswith("(") and s.endswith(")")
        compact = s.replace(" ", "").replace("$", "").replace("€", "").replace("£", "").strip("()")
        if not re.fullmatch(r"-?[\d,]*\.?\d+", compact):
            raise ParseError(f"not a {vtype.value}: {s[:40]!r}")
        try:
            num = Decimal(compact.replace(",", ""))
        except InvalidOperation as e:
            raise ParseError(str(e)) from e
        num = -num if neg else num
        if vtype == ValueType.integer:
            if num != num.to_integral_value():
                raise ParseError(f"not an integer: {s[:40]!r}")
            return int(num)
        return str(num)
    if vtype == ValueType.boolean:
        low = s.lower()
        if low in ("yes", "true", "y", "1"):
            return True
        if low in ("no", "false", "n", "0"):
            return False
        raise ParseError(f"not a boolean: {s[:40]!r}")
    if not s:
        raise ParseError("empty value")
    return s


def normalize(value: Any, how: str) -> str | None:
    s = "" if value is None else str(value)
    if how == "digits":
        return re.sub(r"\D", "", s)
    if how == "decimal":
        try:
            return str(Decimal(parse_value(s, ValueType.decimal)).normalize())
        except (ParseError, InvalidOperation):
            return None
    return re.sub(r"\s+", " ", s).strip().rstrip(":").strip().lower()


def matches(displayed: Any, *, equals: str | None, pattern: str | None, how: str) -> bool:
    if pattern is not None and re.fullmatch(pattern, str(displayed).strip()) is None:
        return False
    if equals is not None:
        a, b = normalize(displayed, how), normalize(equals, how)
        return a is not None and a == b
    return True
