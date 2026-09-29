"""Send pipeline run results to the API's signed event-ingestion endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from datawarden.config import get_settings
from datawarden.contracts.events import IncidentEventIn, RunSummary
from datawarden.events import signing


def build_events(report) -> list[tuple[str, IncidentEventIn]]:
    s = report.summary()
    run = RunSummary(
        run_id=s["run_id"],
        status=s["status"],
        trigger=s["trigger"],
        code_commit=s["code_commit"],
        source_watermark=s["source_watermark"],
        tasks=s["tasks"],
        error=s["error"],
        checks=s["checks"],
    )
    now = datetime.now(UTC)
    events = [
        (
            f"{report.run_id}:run",
            IncidentEventIn(source="pipeline", event_type="pipeline_run", occurred_at=now, run=run),
        )
    ]
    for c in report.failing_checks():
        events.append(
            (
                f"{report.run_id}:{c.check_id}",
                IncidentEventIn(
                    source="pipeline",
                    event_type="check_result",
                    occurred_at=now,
                    run=run,
                    check_id=c.check_id,
                    check_type=c.check_type,
                    asset=c.asset,
                    status=c.status,
                    severity=c.severity,
                    message=c.message[:2000],
                    observed=json.loads(json.dumps(c.observed, default=str)),
                    partition_date=c.partition_date,
                ),
            )
        )
    return events


def post_event(client: httpx.Client, key: str, event: IncidentEventIn, base_url: str | None = None) -> dict:
    s = get_settings()
    body = event.model_dump_json().encode()
    url = f"{(base_url or s.api_base_url).rstrip('/')}/api/v1/events"
    resp = client.post(url, content=body, headers=signing.headers(s.ingest_hmac_secret.get_secret_value(), body, key))
    resp.raise_for_status()
    return resp.json()


def emit_run_events(report, client: httpx.Client | None = None, base_url: str | None = None) -> dict:
    own = client is None
    client = client or httpx.Client(timeout=15)
    incidents: set[str] = set()
    try:
        for key, event in build_events(report):
            res = post_event(client, key, event, base_url)
            if res.get("incident_id"):
                incidents.add(res["incident_id"])
    finally:
        if own:
            client.close()
    return {"events_sent": len(build_events(report)), "incidents": sorted(incidents)}
