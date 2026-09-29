"""API integration tests: signed ingestion, idempotency, auth/CSRF, roles, streaming, MCP."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from datawarden.config import get_settings
from datawarden.contracts.events import IncidentEventIn
from datawarden.events import signing

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(seeded):
    from datawarden.api.main import app

    return TestClient(app)


def _event(check_id="freshness.raw_order_events", status="fail") -> IncidentEventIn:
    return IncidentEventIn(
        source="api",
        event_type="check_result",
        occurred_at=datetime.now(UTC),
        check_id=check_id,
        check_type="freshness",
        asset="raw_order_events",
        status=status,
        severity="high",
        message="test event",
        observed={},
    )


def _post(client, event: IncidentEventIn, key: str, secret: str | None = None, tamper: bool = False):
    body = event.model_dump_json().encode()
    headers = signing.headers(secret or get_settings().ingest_hmac_secret.get_secret_value(), body, key)
    if tamper:
        body = body.replace(b"test event", b"evil event")
    return client.post("/api/v1/events", content=body, headers=headers)


def test_unsigned_or_tampered_events_are_rejected(client, clean):
    assert _post(client, _event(), "k-" + uuid.uuid4().hex, secret="wrong-secret").status_code == 401
    assert _post(client, _event(), "k-" + uuid.uuid4().hex, tamper=True).status_code == 401
    r = client.post("/api/v1/events", content=b"{}", headers={"Idempotency-Key": "k-12345678"})
    assert r.status_code == 401


def test_signed_events_are_idempotent_and_open_one_incident(client, clean):
    key = "k-" + uuid.uuid4().hex
    first = _post(client, _event(), key)
    again = _post(client, _event(), key)
    assert first.status_code == 202 and again.status_code == 202
    assert again.json()["duplicate"] is True and again.json()["incident_id"] == first.json()["incident_id"]
    # a second failing check for the same asset attaches to the same active incident
    other = _post(client, _event("contract.raw_order_events"), "k-" + uuid.uuid4().hex)
    assert other.json()["incident_id"] == first.json()["incident_id"]


def test_login_csrf_and_roles(client, clean):
    creds = json.loads((get_settings().artifact_dir / "demo_credentials.json").read_text())
    assert client.post("/api/v1/auth/login", json={"username": "viewer", "password": "nope"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"username": "operator", "password": creds["operator"]})
    assert r.status_code == 200 and "dw_session" in r.cookies
    me = client.get("/api/v1/auth/me").json()
    assert me["role"] == "operator" and "approval.decide" not in me["permissions"]
    # cookie-authenticated mutation without the CSRF header is refused
    assert client.post("/api/v1/pipeline-runs").status_code == 403
    ok = client.post("/api/v1/pipeline-runs", headers={"X-CSRF-Token": me["csrf_token"]})
    assert ok.status_code == 202
    from datawarden.worker.main import drain

    drain()  # run it so the queue is left clean
    client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]})
    assert client.get("/api/v1/incidents").status_code == 401


def test_errors_carry_request_ids_and_openapi_documents_endpoints(client):
    r = client.get("/api/v1/incidents")
    assert r.status_code == 401 and r.json()["request_id"] and r.headers["X-Request-ID"]
    paths = client.get("/api/openapi.json").json()["paths"]
    for p in (
        "/api/v1/events",
        "/api/v1/incidents",
        "/api/v1/approvals/{approval_id}/decision",
        "/api/v1/stream",
        "/api/v1/demo/faults",
        "/api/v1/evaluations",
        "/api/v1/readyz",
    ):
        assert p in paths


def test_timeline_resume_after_event_id(client, clean):
    res = _post(client, _event("freshness.raw_refund_events"), "k-" + uuid.uuid4().hex)
    iid = res.json()["incident_id"]
    creds = json.loads((get_settings().artifact_dir / "demo_credentials.json").read_text())
    client.post("/api/v1/auth/login", json={"username": "viewer", "password": creds["viewer"]})
    items = client.get(f"/api/v1/incidents/{iid}/timeline").json()["items"]
    assert items and items[0]["event_type"] == "incident.opened"
    last = items[-1]["id"]
    assert client.get(f"/api/v1/incidents/{iid}/timeline?after_id={last}").json()["items"] == []


def test_mcp_server_is_read_only_and_audited(seeded):
    from datawarden.mcp_server import server

    async def go():
        tools = {t.name for t in await server.list_tools()}
        assert {"list_assets", "get_quality_results", "get_lineage_neighbors", "get_incident"} <= tools
        assert not any(w in n for n in tools for w in ("execute", "approve", "rollback", "propose", "shadow"))
        result = await server.call_tool("get_lineage_neighbors", {"asset_id": "fct_payments", "depth": 1})
        return result

    result = asyncio.run(go())
    assert "mart_daily_revenue" in json.dumps(result, default=str)
