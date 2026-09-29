"""Assets, lineage, checks, and pipeline runs (read) + manual pipeline run requests."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datawarden.api.deps import Page, Principal, get_db, page, request_id, require
from datawarden.api.serialize import row, rows
from datawarden.db.models import Asset, LineageEdge, PipelineRun, QualityCheck, QualityResult
from datawarden.services import jobs
from datawarden.services.audit import audit
from datawarden.tools import readonly
from datawarden.tools.base import ToolContext, ToolError

router = APIRouter(tags=["catalog"])
reader = require("read")


@router.get("/assets")
def list_assets(db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    assets = rows(db.scalars(select(Asset).order_by(Asset.id)))
    latest = {}
    for r in db.execute(
        select(QualityResult.check_id, QualityResult.status, QualityCheck.asset_id)
        .join(QualityCheck, QualityCheck.id == QualityResult.check_id)
        .where(QualityResult.id.in_(select(func.max(QualityResult.id)).group_by(QualityResult.check_id)))
    ):
        if r.asset_id:
            latest.setdefault(r.asset_id, []).append({"check_id": r.check_id, "status": r.status})
    for a in assets:
        a["recent_check_failures"] = [c for c in latest.get(a["id"], []) if c["status"] != "pass"]
    return {"items": assets}


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "asset not found")
    checks = rows(db.scalars(select(QualityCheck).where(QualityCheck.asset_id == asset_id)))
    try:
        live = readonly.get_asset_metadata(ToolContext("api"), readonly.AssetArgs(asset_id=asset_id))
    except Exception as exc:  # noqa: BLE001
        live = {"error": f"warehouse unavailable: {type(exc).__name__}"}
    up = [
        e.upstream_asset_id for e in db.scalars(select(LineageEdge).where(LineageEdge.downstream_asset_id == asset_id))
    ]
    down = [
        e.downstream_asset_id for e in db.scalars(select(LineageEdge).where(LineageEdge.upstream_asset_id == asset_id))
    ]
    return {**row(asset), "checks": checks, "live": live, "upstream": up, "downstream": down}


@router.get("/lineage")
def lineage(db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    return {"nodes": rows(db.scalars(select(Asset))), "edges": rows(db.scalars(select(LineageEdge)))}


@router.get("/checks")
def list_checks(db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    checks = rows(db.scalars(select(QualityCheck).order_by(QualityCheck.id)))
    last = {
        r.check_id: r
        for r in db.scalars(
            select(QualityResult).where(
                QualityResult.id.in_(select(func.max(QualityResult.id)).group_by(QualityResult.check_id))
            )
        )
    }
    for c in checks:
        r = last.get(c["id"])
        c["latest"] = row(r) if r else None
    return {"items": checks}


@router.get("/checks/{check_id}/results")
def check_results(
    check_id: str, p: Page = Depends(page), db: Session = Depends(get_db), _: Principal = Depends(reader)
) -> dict:
    q = select(QualityResult).where(QualityResult.check_id == check_id)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = rows(db.scalars(q.order_by(QualityResult.id.desc()).limit(p.limit).offset(p.offset)))
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


@router.get("/pipeline-runs")
def list_runs(p: Page = Depends(page), db: Session = Depends(get_db), _: Principal = Depends(reader)) -> dict:
    total = db.scalar(select(func.count()).select_from(PipelineRun))
    items = rows(
        db.scalars(select(PipelineRun).order_by(PipelineRun.reported_at.desc()).limit(p.limit).offset(p.offset))
    )
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


@router.get("/pipeline-runs/{run_id}")
def get_run(run_id: str, task: str | None = Query(None, max_length=48), _: Principal = Depends(reader)) -> dict:
    ctx = ToolContext("api")
    try:
        run = readonly.get_pipeline_run(ctx, readonly.RunArgs(run_id=run_id))
        logs = readonly.get_pipeline_logs(ctx, readonly.LogArgs(run_id=run_id, task=task, limit=100))
    except ToolError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"run": run["run"], "tasks": run["tasks"], "logs": logs["lines"]}


@router.post("/pipeline-runs", status_code=202)
def request_run(
    db: Session = Depends(get_db), user: Principal = Depends(require("pipeline.run")), rid: str = Depends(request_id)
) -> dict:
    job_id = jobs.enqueue(db, "pipeline_run", {"trigger": "manual_api", "requested_by": user.username})
    audit(db, user.actor, "pipeline.run_requested", "job", job_id, request_id=rid)
    return {"job_id": job_id}
