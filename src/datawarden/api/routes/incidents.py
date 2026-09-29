"""Incidents, evidence, agent activity, proposals, approvals, recovery, and live streaming."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from datawarden.api.deps import Page, Principal, get_db, idempotency_key, page, request_id, require
from datawarden.api.serialize import row, rows
from datawarden.db.models import (
    AgentRun,
    Approval,
    Evidence,
    Hypothesis,
    Incident,
    IncidentEvent,
    ModelUsage,
    RecoveryOperation,
    RepairProposal,
    StreamEvent,
    ToolCall,
    Validation,
)
from datawarden.db.session import new_session
from datawarden.services import approvals, incidents

router = APIRouter(tags=["incidents"])
reader = require("read")


def _incident(db: Session, incident_id: str) -> Incident:
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return inc


@router.get("/incidents")
def list_incidents(
    p: Page = Depends(page),
    status: str | None = Query(None, max_length=32),
    severity: str | None = Query(None, pattern="^(critical|high|warning)$"),
    db: Session = Depends(get_db),
    _: Principal = Depends(reader),
) -> dict:
    q = select(Incident)
    if status == "active":
        q = q.where(Incident.status.in_(incidents.ACTIVE_STATUSES))
    elif status:
        q = q.where(Incident.status == status)
    if severity:
        q = q.where(Incident.severity == severity)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = rows(db.scalars(q.order_by(Incident.created_at.desc()).limit(p.limit).offset(p.offset)))
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    inc = _incident(db, incident_id)
    proposals = []
    for prop in db.scalars(
        select(RepairProposal).where(RepairProposal.incident_id == incident_id).order_by(RepairProposal.revision)
    ):
        d = row(prop)
        d["validations"] = rows(
            db.scalars(select(Validation).where(Validation.proposal_id == prop.id).order_by(Validation.id))
        )
        appr = db.scalar(select(Approval).where(Approval.proposal_id == prop.id))
        if appr:
            approvals.expire_if_due(db, appr)
        d["approval"] = row(appr) if appr else None
        proposals.append(d)
    usage = db.execute(
        select(
            func.coalesce(func.sum(ModelUsage.input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.output_tokens), 0),
            func.sum(ModelUsage.estimated_cost_usd),
            func.count(ModelUsage.id),
        ).where(ModelUsage.incident_id == incident_id)
    ).one()
    return {
        "incident": row(inc),
        "events": rows(
            db.scalars(
                select(IncidentEvent)
                .where(IncidentEvent.incident_id == incident_id)
                .order_by(IncidentEvent.received_at)
            )
        ),
        "agent_runs": rows(
            db.scalars(select(AgentRun).where(AgentRun.incident_id == incident_id).order_by(AgentRun.started_at))
        ),
        "hypotheses": rows(
            db.scalars(select(Hypothesis).where(Hypothesis.incident_id == incident_id).order_by(Hypothesis.created_at))
        ),
        "evidence_count": db.scalar(
            select(func.count()).select_from(Evidence).where(Evidence.incident_id == incident_id)
        ),
        "proposals": proposals,
        "recovery_operations": rows(
            db.scalars(
                select(RecoveryOperation)
                .where(RecoveryOperation.incident_id == incident_id)
                .order_by(RecoveryOperation.started_at)
            )
        ),
        "model_usage": {
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "estimated_cost_usd": float(usage[2]) if usage[2] is not None else None,
            "calls": usage[3],
        },
    }


@router.get("/incidents/{incident_id}/evidence")
def list_evidence(
    incident_id: str, p: Page = Depends(page), db: Session = Depends(get_db), _: Principal = Depends(reader)
) -> dict:
    q = select(Evidence).where(Evidence.incident_id == incident_id)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = rows(db.scalars(q.order_by(Evidence.collected_at).limit(p.limit).offset(p.offset)))
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    ev = db.get(Evidence, evidence_id)
    if ev is None:
        raise HTTPException(404, "evidence not found")
    return row(ev)


@router.get("/incidents/{incident_id}/tool-calls")
def list_tool_calls(
    incident_id: str, p: Page = Depends(page), db: Session = Depends(get_db), _: Principal = Depends(reader)
) -> dict:
    q = select(ToolCall).where(ToolCall.incident_id == incident_id)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = rows(db.scalars(q.order_by(ToolCall.started_at).limit(p.limit).offset(p.offset)), exclude={"output"})
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


@router.get("/incidents/{incident_id}/timeline")
def timeline(
    incident_id: str,
    after_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    _: Principal = Depends(reader),
) -> dict:
    items = rows(
        db.scalars(
            select(StreamEvent)
            .where(StreamEvent.incident_id == incident_id, StreamEvent.id > after_id)
            .order_by(StreamEvent.id)
            .limit(limit)
        )
    )
    return {"items": items}


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/incidents/{incident_id}/start", status_code=202)
def start(
    incident_id: str,
    db: Session = Depends(get_db),
    user: Principal = Depends(require("incident.start")),
    rid: str = Depends(request_id),
) -> dict:
    inc = _incident(db, incident_id)
    try:
        job_id = incidents.start_investigation(db, inc, user.actor, rid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job_id, "incident_id": incident_id}


@router.post("/incidents/{incident_id}/cancel")
def cancel(
    incident_id: str,
    body: CancelIn,
    db: Session = Depends(get_db),
    user: Principal = Depends(require("incident.cancel")),
    rid: str = Depends(request_id),
) -> dict:
    inc = _incident(db, incident_id)
    try:
        incidents.cancel(db, inc, user.actor, body.reason, rid)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"incident_id": incident_id, "status": inc.status}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    prop = db.get(RepairProposal, proposal_id)
    if prop is None:
        raise HTTPException(404, "proposal not found")
    appr = db.scalar(select(Approval).where(Approval.proposal_id == proposal_id))
    return {
        **row(prop),
        "validations": rows(
            db.scalars(select(Validation).where(Validation.proposal_id == proposal_id).order_by(Validation.id))
        ),
        "approval": row(appr) if appr else None,
    }


@router.get("/approvals")
def list_approvals(
    status: str | None = Query(None, pattern="^(pending|approved|rejected|expired|invalidated)$"),
    p: Page = Depends(page),
    db: Session = Depends(get_db),
    _: Principal = Depends(reader),
) -> dict:
    q = select(Approval)
    if status:
        q = q.where(Approval.status == status)
    items = list(db.scalars(q.order_by(Approval.requested_at.desc()).limit(p.limit).offset(p.offset)))
    for a in items:
        approvals.expire_if_due(db, a)
    return {"items": rows(items)}


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    version: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=1000)


@router.post("/approvals/{approval_id}/decision")
def decide(
    approval_id: str,
    body: DecisionIn,
    db: Session = Depends(get_db),
    user: Principal = Depends(require("approval.decide")),
    rid: str = Depends(request_id),
    key: str | None = Depends(idempotency_key),
) -> dict:
    if key is None:
        raise HTTPException(400, "Idempotency-Key header required for approval decisions")
    try:
        return approvals.decide(
            db,
            approval_id,
            body.decision,
            body.version,
            user_id=user.user_id,
            actor=user.actor,
            comment=body.comment,
            request_id=rid,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except approvals.ApprovalConflict as exc:
        db.commit()  # persist expiry transitions
        raise HTTPException(409, str(exc)) from exc


@router.get("/incidents/{incident_id}/recovery")
def recovery(incident_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    return {
        "items": rows(
            db.scalars(
                select(RecoveryOperation)
                .where(RecoveryOperation.incident_id == incident_id)
                .order_by(RecoveryOperation.started_at)
            )
        )
    }


@router.get("/recovery-operations")
def list_recovery(p: Page = Depends(page), db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    return {
        "items": rows(
            db.scalars(
                select(RecoveryOperation).order_by(RecoveryOperation.started_at.desc()).limit(p.limit).offset(p.offset)
            )
        )
    }


def _fetch_stream(after: int, incident_id: str | None) -> list[dict]:
    with new_session() as db:
        q = select(StreamEvent).where(StreamEvent.id > after)
        if incident_id:
            q = q.where(StreamEvent.incident_id == incident_id)
        return rows(db.scalars(q.order_by(StreamEvent.id).limit(200)))


@router.get("/stream")
async def stream(
    request: Request,
    incident_id: str | None = Query(None, max_length=40),
    last_event_id: int | None = Query(None, ge=0),
    _: Principal = Depends(reader),
):
    """Server-sent events. Resumes after the ``Last-Event-ID`` header (or ``last_event_id``)."""
    header = request.headers.get("last-event-id")
    after = int(header) if header and header.isdigit() else (last_event_id or 0)

    async def gen():
        nonlocal after
        idle = 0
        while not await request.is_disconnected():
            items = await run_in_threadpool(_fetch_stream, after, incident_id)
            for it in items:
                after = it["id"]
                yield {
                    "id": str(it["id"]),
                    "event": it["event_type"],
                    "data": json.dumps(
                        {"incident_id": it["incident_id"], "payload": it["payload"], "created_at": it["created_at"]}
                    ),
                }
            idle = 0 if items else idle + 1
            await asyncio.sleep(1.0)
            if idle > 1800:
                break

    return EventSourceResponse(gen(), ping=15)


@router.get("/graph/definition")
def graph_definition(variant: str = Query("multi", pattern="^(multi|single)$"), _: Principal = Depends(reader)) -> dict:
    """The incident *execution* graph (not data lineage)."""
    from datawarden.agents.graph import EDGES, GRAPH_VERSION, NODES

    if variant == "single":
        nodes = [n for n in NODES if n not in ("quality_investigator", "lineage_investigator", "root_cause")]
        nodes.insert(2, "single_investigation")
        edges = [e for e in EDGES if not set(e) & {"quality_investigator", "lineage_investigator", "root_cause"}]
        edges += [
            ("load_context", "single_investigation"),
            ("single_investigation", "repair_planner"),
            ("single_investigation", "close_no_action"),
            ("single_investigation", "escalate"),
        ]
    else:
        nodes, edges = NODES, EDGES
    return {
        "version": GRAPH_VERSION,
        "variant": variant,
        "nodes": nodes,
        "edges": [{"source": a, "target": b} for a, b in edges],
    }


@router.get("/incidents/{incident_id}/graph-status")
def graph_status(incident_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    inc = _incident(db, incident_id)
    status: dict[str, dict] = {}
    for ev in db.scalars(
        select(StreamEvent)
        .where(
            StreamEvent.incident_id == incident_id,
            StreamEvent.event_type.in_(["node.started", "node.completed", "node.failed"]),
        )
        .order_by(StreamEvent.id)
    ):
        node_name = ev.payload.get("node")
        cur = status.setdefault(node_name, {"runs": 0})
        if ev.event_type == "node.started":
            cur.update(state="running", runs=cur["runs"] + 1, started_at=ev.created_at.isoformat())
        elif ev.event_type == "node.completed":
            cur.update(state="completed", seconds=ev.payload.get("seconds"), route=ev.payload.get("route"))
        else:
            cur.update(state="failed", error=ev.payload.get("error"))
    if inc.status == "awaiting_approval" and "request_approval" in status:
        status["request_approval"]["state"] = "waiting"
    variant = inc.graph_version.split("+", 1)[1] if inc.graph_version and "+" in inc.graph_version else "multi"
    return {"incident_status": inc.status, "variant": variant, "nodes": status}


@router.get("/proposals/{proposal_id}/comparison")
def proposal_comparison(proposal_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    """Shadow vs canonical mart partitions for a proposal. Live while the shadow schema exists;
    otherwise the comparison captured at validation time."""
    from datawarden.recovery.validation import shadow_vs_canonical
    from datawarden.warehouse.conn import connect

    prop = db.get(RepairProposal, proposal_id)
    if prop is None:
        raise HTTPException(404, "proposal not found")
    if prop.shadow_schema:
        try:
            with connect("validator") as conn:
                live = conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{prop.shadow_schema}.mart_daily_revenue",)
                ).fetchone()["ok"]
            if live:
                return {
                    "proposal_id": proposal_id,
                    **shadow_vs_canonical(prop.shadow_schema, prop.partition_scope),
                    "captured_at": "live",
                }
        except Exception:  # noqa: BLE001 - fall back to the comparison captured at validation time
            stored = prop.comparison or {}
            return {
                "proposal_id": proposal_id,
                "shadow_schema": prop.shadow_schema,
                "shadow_available": False,
                "partitions_compared": stored.get("partitions_compared", 0),
                "differences": stored.get("differences", []),
                "captured_at": "validation",
            }
    stored = prop.comparison or {}
    return {
        "proposal_id": proposal_id,
        "shadow_schema": prop.shadow_schema,
        "shadow_available": bool(stored.get("differences") is not None and not stored.get("error")),
        "partitions_compared": stored.get("partitions_compared", 0),
        "differences": stored.get("differences", []),
        "captured_at": "validation" if stored else None,
    }
