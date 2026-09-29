from __future__ import annotations

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
    """Seed once per session (deterministic), then give every test a clean, healthy state."""
    from datawarden.cli import main

    assert main(["seed"]) == 0
    return warehouse


@pytest.fixture
def clean(seeded):
    from datawarden.faults import injector

    if injector.load_state()["active"]:
        injector.reset(rebuild=True)
    yield seeded
    if injector.load_state()["active"]:
        injector.reset(rebuild=True)
