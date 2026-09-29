"""ORM -> JSON helpers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.inspection import inspect


def row(obj, exclude: set[str] | None = None) -> dict:
    out = {}
    for attr in inspect(obj).mapper.column_attrs:
        k = attr.key
        if exclude and k in exclude:
            continue
        v = getattr(obj, k)
        if isinstance(v, datetime | date):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        out[k] = v
    return out


def rows(objs, exclude: set[str] | None = None) -> list[dict]:
    return [row(o, exclude) for o in objs]
