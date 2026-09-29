"""Append-only audit records and persisted live-progress (SSE) events."""

from __future__ import annotations

from sqlalchemy.orm import Session

from datawarden.db.models import AuditEvent, StreamEvent


def audit(
    session: Session,
    actor: str,
    action: str,
    target_type: str,
    target_id: str | None = None,
    *,
    incident_id: str | None = None,
    detail: dict | None = None,
    request_id: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            incident_id=incident_id,
            detail=detail or {},
            request_id=request_id,
        )
    )


def publish(session: Session, incident_id: str | None, event_type: str, payload: dict | None = None) -> None:
    """Persist an operational progress event. Streams read these rows; ids are SSE event ids."""
    session.add(StreamEvent(incident_id=incident_id, event_type=event_type, payload=payload or {}))
