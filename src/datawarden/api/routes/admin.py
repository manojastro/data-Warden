"""Demo controls (synthetic only), evaluations, audit history, usage, health and integrations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from datawarden.api.deps import Page, Principal, get_db, page, request_id, require
from datawarden.api.serialize import row, rows
from datawarden.config import get_settings
from datawarden.db.models import AuditEvent, EvaluationRun, Job, ModelUsage, WorkerHeartbeat
from datawarden.services import integrations, jobs
from datawarden.services.audit import audit

router = APIRouter(tags=["admin"])
reader = require("read")


def _demo_only() -> None:
    if not get_settings().demo_mode:
        raise HTTPException(403, "demo controls are disabled outside demo mode")


@router.get("/demo/scenarios")
def scenarios(_: Principal = Depends(reader)) -> dict:
    from datawarden.faults.injector import SCENARIOS, load_state

    active = [f["scenario"] for f in load_state()["active"]]
    return {
        "synthetic": True,
        "demo_mode": get_settings().demo_mode,
        "active": active,
        "items": [{"id": k, "description": v} for k, v in SCENARIOS.items()],
    }


class FaultIn(BaseModel):
    scenario: str
    run_pipeline: bool = True


@router.post("/demo/faults", status_code=202)
def inject_fault(
    body: FaultIn,
    db: Session = Depends(get_db),
    user: Principal = Depends(require("demo.control")),
    rid: str = Depends(request_id),
) -> dict:
    _demo_only()
    from datawarden.faults.injector import SCENARIOS

    if body.scenario not in SCENARIOS:
        raise HTTPException(422, "unknown scenario")
    job_id = jobs.enqueue(
        db, "demo_inject", {"scenario": body.scenario, "run_pipeline": body.run_pipeline}, max_attempts=1
    )
    audit(db, user.actor, "demo.fault_requested", "scenario", body.scenario, request_id=rid)
    return {"job_id": job_id}


@router.post("/demo/reset", status_code=202)
def reset(
    db: Session = Depends(get_db), user: Principal = Depends(require("demo.control")), rid: str = Depends(request_id)
) -> dict:
    _demo_only()
    job_id = jobs.enqueue(db, "demo_reset", {}, max_attempts=1)
    audit(db, user.actor, "demo.reset_requested", "demo", None, request_id=rid)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return row(job)


@router.get("/jobs")
def list_jobs(p: Page = Depends(page), db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    return {"items": rows(db.scalars(select(Job).order_by(Job.created_at.desc()).limit(p.limit).offset(p.offset)))}


class EvalIn(BaseModel):
    scenarios: list[str] | None = None
    seeds: list[int] | None = None
    modes: list[str] | None = None


@router.post("/evaluations", status_code=202)
def start_eval(
    body: EvalIn,
    db: Session = Depends(get_db),
    user: Principal = Depends(require("eval.run")),
    rid: str = Depends(request_id),
) -> dict:
    _demo_only()
    job_id = jobs.enqueue(db, "evaluation", body.model_dump(), max_attempts=1)
    audit(db, user.actor, "evaluation.requested", "job", job_id, request_id=rid)
    return {"job_id": job_id}


@router.get("/evaluations")
def list_evals(db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    return {"items": rows(db.scalars(select(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(20)))}


@router.get("/evaluations/{eval_id}")
def get_eval(eval_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    ev = db.get(EvaluationRun, eval_id)
    if ev is None:
        raise HTTPException(404, "evaluation not found")
    return row(ev)


@router.get("/audit")
def audit_log(
    p: Page = Depends(page),
    incident_id: str | None = Query(None, max_length=40),
    action: str | None = Query(None, max_length=64),
    db: Session = Depends(get_db),
    _: Principal = Depends(reader),
) -> dict:
    q = select(AuditEvent)
    if incident_id:
        q = q.where(AuditEvent.incident_id == incident_id)
    if action:
        q = q.where(AuditEvent.action.startswith(action))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = rows(db.scalars(q.order_by(AuditEvent.id.desc()).limit(p.limit).offset(p.offset)))
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


@router.get("/usage")
def usage(db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    q = select(
        ModelUsage.incident_id,
        ModelUsage.mode,
        func.sum(ModelUsage.input_tokens),
        func.sum(ModelUsage.output_tokens),
        func.sum(ModelUsage.estimated_cost_usd),
        func.count(ModelUsage.id),
        func.sum(ModelUsage.latency_ms),
    ).group_by(ModelUsage.incident_id, ModelUsage.mode)
    return {
        "items": [
            {
                "incident_id": r[0],
                "mode": r[1],
                "input_tokens": r[2],
                "output_tokens": r[3],
                "estimated_cost_usd": float(r[4]) if r[4] is not None else None,
                "calls": r[5],
                "model_latency_ms": r[6],
            }
            for r in db.execute(q)
        ]
    }


@router.get("/integrations")
def integration_status(_: Principal = Depends(reader)) -> dict:
    return {"items": integrations.status()}


health = APIRouter(tags=["health"])


@health.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@health.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict:
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["app_db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["app_db"] = f"error: {type(exc).__name__}"
    wh = next((i for i in integrations.status() if i["name"] == "warehouse"), {"status": "unknown"})
    checks["warehouse"] = "ok" if wh["status"] == "connected" else wh["status"]
    try:
        hb = db.scalar(select(func.max(WorkerHeartbeat.seen_at)))
        fresh = hb is not None and hb > datetime.now(UTC) - timedelta(seconds=60)
        checks["worker"] = "ok" if fresh else "no recent heartbeat"
    except Exception:  # noqa: BLE001
        checks["worker"] = "unknown"
    checks["artifacts"] = "ok" if get_settings().artifact_dir.exists() else "missing"
    ready = checks["app_db"] == "ok" and checks["warehouse"] == "ok"
    if not ready:
        raise HTTPException(503, {"ready": False, "checks": checks})
    return {"ready": True, "checks": checks}
