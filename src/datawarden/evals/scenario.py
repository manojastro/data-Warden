"""Drive one fault scenario through the real platform path.

reset -> inject -> pipeline run -> signed events via the API -> worker jobs (graph) ->
approval via the API as the approver (if requested) -> worker jobs (execution) -> outcome.

Uses the in-process ASGI app through ``TestClient`` (an httpx client), so events and approvals
take the same HTTP route, signature checks, and permission checks as the live stack.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from fastapi.testclient import TestClient
from sqlalchemy import select

from datawarden.config import get_settings
from datawarden.db.models import Approval, Incident, RecoveryOperation, RepairProposal, ToolCall, Validation
from datawarden.db.session import new_session
from datawarden.events.emitter import emit_run_events
from datawarden.faults import injector
from datawarden.pipeline.runner import run_pipeline
from datawarden.worker.main import drain


@dataclass
class ScenarioOutcome:
    scenario: str
    detected: bool
    incident_ids: list[str] = field(default_factory=list)
    status: str | None = None
    terminal_reason: str | None = None
    root_cause: str | None = None
    proposals: list[dict] = field(default_factory=list)
    approval: str | None = None
    recovery: str | None = None
    failing_checks_before: list[str] = field(default_factory=list)
    failing_checks_after: list[str] = field(default_factory=list)
    tool_calls: int = 0
    denied_tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    investigation_seconds: float = 0.0
    approval_wait_seconds: float = 0.0
    recovery_seconds: float = 0.0
    ground_truth: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


_client: TestClient | None = None


def api_client() -> TestClient:
    global _client
    if _client is None:
        from datawarden.api.main import app

        _client = TestClient(app)
    return _client


def login(client: TestClient, role: str) -> str:
    creds = json.loads((get_settings().artifact_dir / "demo_credentials.json").read_text())
    r = client.post("/api/v1/auth/login", json={"username": role, "password": creds[role]})
    r.raise_for_status()
    return r.json()["csrf_token"]


def decide(client: TestClient, approval_id: str, decision: str = "approve", role: str = "approver") -> dict:
    csrf = login(client, role)
    appr = client.get(f"/api/v1/proposals/{_proposal_of(approval_id)}").json()["approval"]
    r = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": decision, "version": appr["version"], "comment": f"eval {decision}"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid.uuid4().hex},
    )
    return {"status_code": r.status_code, **r.json()}


def _proposal_of(approval_id: str) -> str:
    with new_session() as db:
        return db.get(Approval, approval_id).proposal_id


def pending_approval(incident_id: str) -> Approval | None:
    with new_session() as db:
        return db.scalar(select(Approval).where(Approval.incident_id == incident_id, Approval.status == "pending"))


def run_scenario(
    scenario: str | None, *, approve: bool = True, reset_first: bool = True, approval_delay_s: float = 0.0
) -> ScenarioOutcome:
    client = api_client()
    if reset_first and injector.load_state()["active"]:
        injector.reset(rebuild=True)
    if scenario and scenario != "healthy":
        injector.inject(scenario)
    truth = (injector.load_state()["active"] or [{}])[0].get("ground_truth", {})
    report = run_pipeline(trigger="scheduled")
    emitted = emit_run_events(report, client=client, base_url="http://testserver")
    out = ScenarioOutcome(
        scenario or "healthy",
        detected=bool(emitted["incidents"]),
        incident_ids=emitted["incidents"],
        failing_checks_before=[c.check_id for c in report.failing_checks()],
        ground_truth=truth,
    )
    if not emitted["incidents"]:
        return out
    iid = emitted["incidents"][0]
    t0 = time.monotonic()
    drain(wait_for_delayed=6)
    out.investigation_seconds = round(time.monotonic() - t0, 2)
    appr = pending_approval(iid)
    if appr is not None:
        out.approval = "pending"
        if approve:
            time.sleep(approval_delay_s)
            out.approval_wait_seconds = approval_delay_s
            res = decide(client, appr.id)
            out.approval = res.get("status")
            t1 = time.monotonic()
            drain()
            out.recovery_seconds = round(time.monotonic() - t1, 2)
    _fill(out, iid)
    from datawarden.checks.registry import run_checks
    from datawarden.warehouse.conn import connect

    with connect("validator") as conn:
        out.failing_checks_after = [r.check_id for r in run_checks(conn) if r.status != "pass"]
    return out


def _fill(out: ScenarioOutcome, iid: str) -> None:
    with new_session() as db:
        inc = db.get(Incident, iid)
        out.status, out.terminal_reason = inc.status, inc.terminal_reason
        rc = (inc.summary or {}).get("root_cause") or {}
        out.root_cause = rc.get("category")
        for p in db.scalars(
            select(RepairProposal).where(RepairProposal.incident_id == iid).order_by(RepairProposal.revision)
        ):
            vals = list(db.scalars(select(Validation).where(Validation.proposal_id == p.id)))
            out.proposals.append(
                {
                    "id": p.id,
                    "kind": p.kind,
                    "status": p.status,
                    "scope": p.partition_scope,
                    "policy_violations": (p.policy_result or {}).get("violations", []),
                    "failed_validations": [v.check_id for v in vals if v.status != "pass"],
                    "claimed_confidence": float(p.claimed_confidence) if p.claimed_confidence else None,
                }
            )
        op = db.scalar(
            select(RecoveryOperation)
            .where(RecoveryOperation.incident_id == iid)
            .order_by(RecoveryOperation.started_at.desc())
        )
        out.recovery = op.status if op else None
        calls = list(db.scalars(select(ToolCall).where(ToolCall.incident_id == iid)))
        out.tool_calls = sum(1 for c in calls if c.agent not in ("controller", "executor", "repair_planner"))
        out.denied_tool_calls = sum(1 for c in calls if c.status == "denied")
        usage = inc.usage or {}
        out.input_tokens, out.output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
