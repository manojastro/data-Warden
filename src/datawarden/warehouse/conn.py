"""Warehouse connections, one DSN per scoped database role."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from datawarden.config import get_settings

ROLES = ("admin", "pipeline", "agent_ro", "shadow", "executor", "validator")


def connect(role: str, *, autocommit: bool = False, dict_rows: bool = True) -> psycopg.Connection:
    if role not in ROLES:
        raise ValueError(f"unknown warehouse role {role}")
    return psycopg.connect(
        get_settings().wh_dsn(role),
        autocommit=autocommit,
        row_factory=dict_row if dict_rows else None,
        application_name=f"datawarden-{role}",
        connect_timeout=10,
    )
