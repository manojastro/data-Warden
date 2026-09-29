"""Airflow REST adapter (Airflow 3 API v2). Read runs/logs and trigger scoped replays.

Disabled unless DW_AIRFLOW_BASE_URL is set. Uses the simple auth manager token endpoint.
"""

from __future__ import annotations

import re

import httpx

from datawarden.config import get_settings

DAG_ID = "datawarden_retail_revenue"


class AirflowUnavailable(RuntimeError):
    pass


def _client() -> httpx.Client:
    s = get_settings()
    if not s.airflow_base_url:
        raise AirflowUnavailable("Airflow integration is disabled (DW_AIRFLOW_BASE_URL not set)")
    base = s.airflow_base_url.rstrip("/")
    try:
        r = httpx.post(
            f"{base}/auth/token",
            json={"username": s.airflow_username, "password": s.airflow_password.get_secret_value()},
            timeout=10,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise AirflowUnavailable(f"cannot authenticate to Airflow: {type(exc).__name__}") from exc
    token = r.json()["access_token"]
    return httpx.Client(base_url=f"{base}/api/v2", headers={"Authorization": f"Bearer {token}"}, timeout=20)


def list_runs(limit: int = 10) -> list[dict]:
    with _client() as c:
        r = c.get(f"/dags/{DAG_ID}/dagRuns", params={"limit": min(limit, 50), "order_by": "-logical_date"})
        r.raise_for_status()
        return [
            {k: run.get(k) for k in ("dag_run_id", "state", "logical_date", "start_date", "end_date", "run_type")}
            for run in r.json().get("dag_runs", [])
        ]


def task_log(run_id: str, task_id: str = "run_pipeline", try_number: int = 1) -> str:
    if not re.fullmatch(r"[\w:+.\-]{1,200}", run_id) or not re.fullmatch(r"\w{1,100}", task_id):
        raise ValueError("invalid run or task id")
    with _client() as c:
        r = c.get(
            f"/dags/{DAG_ID}/dagRuns/{run_id}/taskInstances/{task_id}/logs/{int(try_number)}",
            headers={"Accept": "text/plain"},
        )
        r.raise_for_status()
        return r.text[-20_000:]


def trigger(replay_dates: list[str] | None = None, note: str = "") -> dict:
    """Trigger a DAG run; with ``replay_dates`` the run recomputes exactly those partitions."""
    from datetime import UTC, date, datetime

    dates = sorted({date.fromisoformat(d).isoformat() for d in (replay_dates or [])})
    with _client() as c:
        r = c.post(
            f"/dags/{DAG_ID}/dagRuns",
            json={
                "logical_date": datetime.now(UTC).isoformat(),
                "conf": {"replay_dates": ",".join(dates)},
                "note": note[:200],
            },
        )
        r.raise_for_status()
        return {k: r.json().get(k) for k in ("dag_run_id", "state", "logical_date")}
