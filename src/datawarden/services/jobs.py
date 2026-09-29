"""Durable PostgreSQL job queue: leases, heartbeats, SKIP LOCKED claiming, retry with backoff.

The API only enqueues. Workers claim one job at a time with ``FOR UPDATE SKIP LOCKED``; a job
whose lease expired (worker crashed) becomes claimable again, and handlers are idempotent so a
re-run cannot duplicate effects.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from datawarden.db.models import Job, OutboxEvent, new_id

LEASE_SECONDS = 60


def enqueue(
    session: Session,
    kind: str,
    payload: dict | None = None,
    *,
    dedup_key: str | None = None,
    incident_id: str | None = None,
    delay_seconds: float = 0,
    max_attempts: int = 3,
) -> str | None:
    """Insert a job; with ``dedup_key`` a second enqueue is a no-op. Returns job id or None."""
    job_id = new_id("job")
    stmt = insert(Job).values(
        id=job_id,
        kind=kind,
        payload=payload or {},
        status="queued",
        dedup_key=dedup_key,
        incident_id=incident_id,
        max_attempts=max_attempts,
        run_after=datetime.now(UTC) + timedelta(seconds=delay_seconds),
    )
    if dedup_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_key"])
    res = session.execute(stmt.returning(Job.id))
    row = res.first()
    return row[0] if row else None


def outbox(session: Session, topic: str, payload: dict) -> None:
    session.add(OutboxEvent(topic=topic, payload=payload))


def claim(session: Session, worker_id: str, kinds: list[str] | None = None) -> dict | None:
    """Claim the next ready job (or one whose lease expired). Commits the claim."""
    kind_filter = "AND kind = ANY(:kinds)" if kinds else ""
    row = (
        session.execute(
            text(f"""
        WITH next AS (
            SELECT id FROM jobs
            WHERE ((status = 'queued' AND run_after <= now())
                   OR (status = 'running' AND lease_expires_at < now()))
              {kind_filter}
            ORDER BY run_after, created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE jobs j SET status = 'running', lease_owner = :worker,
               lease_expires_at = now() + make_interval(secs => :lease),
               heartbeat_at = now(), attempts = j.attempts + 1
        FROM next WHERE j.id = next.id
        RETURNING j.id, j.kind, j.payload, j.attempts, j.max_attempts, j.incident_id
    """),
            {"worker": worker_id, "lease": LEASE_SECONDS, "kinds": kinds},
        )
        .mappings()
        .first()
    )
    session.commit()
    return dict(row) if row else None


def heartbeat(session: Session, job_id: str, worker_id: str) -> bool:
    res = session.execute(
        text("""
        UPDATE jobs SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :lease)
        WHERE id = :id AND lease_owner = :worker AND status = 'running'"""),
        {"id": job_id, "worker": worker_id, "lease": LEASE_SECONDS},
    )
    session.commit()
    return res.rowcount == 1


def complete(session: Session, job_id: str, worker_id: str, result: dict | None = None) -> None:
    session.execute(
        text("""UPDATE jobs SET status = 'succeeded', finished_at = now(), result = CAST(:r AS jsonb),
                            lease_owner = NULL, lease_expires_at = NULL
                            WHERE id = :id AND lease_owner = :worker"""),
        {"id": job_id, "worker": worker_id, "r": json.dumps(result or {}, default=str)},
    )
    session.commit()


def fail(session: Session, job_id: str, worker_id: str, error: str, attempts: int, max_attempts: int) -> None:
    dead = attempts >= max_attempts
    backoff = min(300, 5 * 2 ** (attempts - 1))
    session.execute(
        text("""UPDATE jobs SET status = :status, last_error = :err, lease_owner = NULL,
                            lease_expires_at = NULL, run_after = now() + make_interval(secs => :backoff),
                            finished_at = CASE WHEN :dead THEN now() ELSE NULL END
                            WHERE id = :id AND lease_owner = :worker"""),
        {
            "status": "dead" if dead else "queued",
            "err": error[:2000],
            "backoff": backoff,
            "dead": dead,
            "id": job_id,
            "worker": worker_id,
        },
    )
    session.commit()


def relay_outbox(session: Session, limit: int = 50) -> int:
    """Move committed outbox events into jobs (idempotent via dedup_key = outbox id)."""
    rows = (
        session.execute(
            text("""SELECT id, topic, payload FROM outbox_events WHERE dispatched_at IS NULL
                                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT :n"""),
            {"n": limit},
        )
        .mappings()
        .all()
    )
    for r in rows:
        payload = r["payload"]
        if r["topic"] == "incident.opened":
            enqueue(
                session,
                "investigate_incident",
                {"incident_id": payload["incident_id"]},
                dedup_key=f"investigate:{payload['incident_id']}",
                incident_id=payload["incident_id"],
                delay_seconds=payload.get("debounce_seconds", 0),
            )
        elif r["topic"] == "approval.decided":
            enqueue(
                session,
                "resume_incident",
                payload,
                dedup_key=f"resume:{payload['approval_id']}:{payload['decision']}",
                incident_id=payload["incident_id"],
            )
        elif r["topic"] == "notify.webhook":
            enqueue(session, "send_webhook", payload, dedup_key=f"webhook:{r['id']}")
        session.execute(text("UPDATE outbox_events SET dispatched_at = now() WHERE id = :id"), {"id": r["id"]})
    session.commit()
    return len(rows)
