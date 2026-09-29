from __future__ import annotations

import logging

import psycopg
import pytest

from datawarden.config import get_settings


def _db_available() -> bool:
    try:
        with psycopg.connect(get_settings().wh_dsn("validator"), connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def warehouse():
    if not _db_available():
        pytest.skip("warehouse not reachable; run `make up` or start PostgreSQL and `make seed`")
    return get_settings()


@pytest.fixture(scope="session")
def seeded(warehouse):
    """Seed once per session (deterministic), sync the catalog, create demo users."""
    from datawarden.cli import main

    logging.disable(logging.WARNING)
    assert main(["seed"]) == 0
    assert main(["app-seed"]) == 0
    _quiesce()
    return warehouse


def _quiesce() -> None:
    """Park leftovers from other sessions so each test only drains its own jobs."""
    from sqlalchemy import text

    from datawarden.db.session import new_session

    with new_session() as db:
        db.execute(
            text(
                "UPDATE jobs SET status = 'dead', last_error = 'parked by test session' "
                "WHERE status IN ('queued', 'running')"
            )
        )
        db.execute(
            text(
                "UPDATE incidents SET status = 'cancelled', terminal_reason = 'parked by test session' "
                "WHERE status IN ('open', 'investigating', 'awaiting_approval')"
            )
        )
        db.execute(text("DELETE FROM partition_locks"))
        db.execute(text("UPDATE outbox_events SET dispatched_at = now() WHERE dispatched_at IS NULL"))
        db.commit()


@pytest.fixture
def clean(seeded):
    from datawarden.faults import injector

    _quiesce()
    if injector.load_state()["active"]:
        injector.reset(rebuild=True)
    yield seeded
    _quiesce()
    if injector.load_state()["active"]:
        injector.reset(rebuild=True)


@pytest.fixture
def settings_override():
    """Temporarily override settings attributes (restored afterwards)."""
    s = get_settings()
    saved = {}

    def apply(**kwargs):
        for k, v in kwargs.items():
            saved.setdefault(k, getattr(s, k))
            setattr(s, k, v)

    yield apply
    for k, v in saved.items():
        setattr(s, k, v)
