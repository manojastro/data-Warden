"""Incident event ingestion, correlation, and lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from datawarden.contracts.events import IncidentEventIn
from datawarden.db.models import Incident, IncidentEvent, PipelineRun, QualityCheck, QualityResult
from datawarden.services import jobs
from datawarden.services.audit import audit, publish
from datawarden.services.catalog import ASSET_INFO, ensure_check

ACTIVE_STATUSES = ("open", "investigating", "awaiting_approval", "recovering")
TERMINAL_STATUSES = ("resolved", "closed_no_action", "escalated", "cancelled", "manual_intervention")
SEVERITY_RANK = {"warning": 1, "high": 2, "critical": 3}
INVESTIGATION_DEBOUNCE_S = 3


def _dates_from(event: IncidentEventIn) -> set[str]:
    dates = set(event.observed.get("mismatched_dates", []) or [])
    dates |= set(event.observed.get("affected_dates", []) or [])
    if event.partition_date:
        dates.add(event.partition_date.isoformat())
    return dates


def _title(event: IncidentEventIn) -> str:
    return f"{event.check_id}: {event.message}"[:300]


def ingest_event(
    db: Session,
    event: IncidentEventIn,
    idempotency_key: str,
    *,
    actor: str,
    request_id: str | None = None,
    auto_start: bool = True,
) -> dict:
    """Store an event exactly once and attach it to an incident when it signals a problem."""
    existing = db.scalar(select(IncidentEvent).where(IncidentEvent.idempotency_key == idempotency_key))
    if existing:
        return {"event_id": existing.id, "incident_id": existing.incident_id, "duplicate": True}

    if event.run:
        stmt = insert(PipelineRun).values(
            id=event.run.run_id,
            trigger=event.run.trigger,
            status=event.run.status,
            source_watermark=event.run.source_watermark,
            code_commit=event.run.code_commit,
            summary=event.run.model_dump(mode="json"),
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["id"], set_={"status": stmt.excluded.status, "summary": stmt.excluded.summary}
            )
        )

    incident: Incident | None = None
    if event.event_type in ("check_result", "pipeline_task_failed") and event.check_id:
        if db.get(QualityCheck, event.check_id) is None:
            ensure_check(db, event.check_id, event.check_type or "operational", event.severity, event.message[:200])
        db.flush()
        db.add(
            QualityResult(
                check_id=event.check_id,
                run_id=event.run.run_id if event.run else None,
                status=event.status or "fail",
                severity=event.severity,
                partition_date=event.partition_date,
                observed=event.observed,
                message=event.message,
            )
        )
        if event.status in ("fail", "warn", "error") or event.event_type == "pipeline_task_failed":
            incident = _correlate(db, event, actor=actor, request_id=request_id, auto_start=auto_start)

    row = IncidentEvent(
        idempotency_key=idempotency_key,
        source=event.source,
        event_type=event.event_type,
        check_id=event.check_id,
        asset_id=event.asset,
        run_id=event.run.run_id if event.run else None,
        severity=event.severity,
        payload=event.model_dump(mode="json"),
        incident_id=incident.id if incident else None,
    )
    db.add(row)
    db.flush()
    return {"event_id": row.id, "incident_id": incident.id if incident else None, "duplicate": False}


def _correlate(
    db: Session, event: IncidentEventIn, *, actor: str, request_id: str | None, auto_start: bool
) -> Incident:
    run_id = event.run.run_id if event.run else None
    key = f"run:{run_id}" if run_id else f"check:{event.check_id}"
    asset = event.asset if event.asset in ASSET_INFO else None
    incident = db.scalar(
        select(Incident).where(Incident.correlation_key == key, Incident.status.in_(ACTIVE_STATUSES)).with_for_update()
    )
    if incident is None and asset:
        # attach to an active incident touching the same asset rather than opening a duplicate
        for inc in db.scalars(select(Incident).where(Incident.status.in_(ACTIVE_STATUSES)).with_for_update()):
            if asset in (inc.asset_ids or []):
                incident = inc
                break
    dates = _dates_from(event)
    if incident is None:
        incident = Incident(
            title=_title(event),
            status="open",
            severity=event.severity,
            asset_ids=[asset] if asset else [],
            check_ids=[event.check_id],
            affected_partitions=sorted(dates),
            run_id=run_id,
            correlation_key=key,
            summary={"opened_by": event.source},
            usage={"tool_calls": 0},
        )
        db.add(incident)
        db.flush()
        audit(
            db,
            actor,
            "incident.opened",
            "incident",
            incident.id,
            incident_id=incident.id,
            detail={"check_id": event.check_id, "run_id": run_id},
            request_id=request_id,
        )
        publish(db, incident.id, "incident.opened", {"title": incident.title, "severity": incident.severity})
        if auto_start:
            jobs.outbox(
                db, "incident.opened", {"incident_id": incident.id, "debounce_seconds": INVESTIGATION_DEBOUNCE_S}
            )
        return incident
    if event.check_id not in (incident.check_ids or []):
        incident.check_ids = [*incident.check_ids, event.check_id]
    if asset and asset not in (incident.asset_ids or []):
        incident.asset_ids = [*incident.asset_ids, asset]
    incident.affected_partitions = sorted(set(incident.affected_partitions or []) | dates)
    if SEVERITY_RANK[event.severity] > SEVERITY_RANK.get(incident.severity, 0):
        incident.severity = event.severity
        incident.title = _title(event)
    publish(db, incident.id, "incident.event_attached", {"check_id": event.check_id})
    return incident


def start_investigation(db: Session, incident: Incident, actor: str, request_id: str | None = None) -> str | None:
    if incident.status not in ("open",):
        raise ValueError(f"incident is {incident.status}; only open incidents can be started")
    job_id = jobs.enqueue(
        db,
        "investigate_incident",
        {"incident_id": incident.id},
        dedup_key=f"investigate:{incident.id}",
        incident_id=incident.id,
    )
    audit(
        db, actor, "incident.start_requested", "incident", incident.id, incident_id=incident.id, request_id=request_id
    )
    return job_id


def cancel(db: Session, incident: Incident, actor: str, reason: str, request_id: str | None = None) -> None:
    if incident.status in TERMINAL_STATUSES:
        raise ValueError(f"incident already {incident.status}")
    if incident.status == "recovering":
        raise ValueError("cannot cancel while a recovery operation is executing")
    incident.status = "cancelled"
    incident.terminal_reason = f"cancelled by {actor}: {reason}"[:500]
    incident.closed_at = datetime.now(UTC)
    incident.version += 1
    audit(
        db,
        actor,
        "incident.cancelled",
        "incident",
        incident.id,
        incident_id=incident.id,
        detail={"reason": reason},
        request_id=request_id,
    )
    publish(db, incident.id, "incident.status", {"status": "cancelled"})
