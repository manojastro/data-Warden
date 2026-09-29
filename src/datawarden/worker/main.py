"""Worker process: claims durable jobs, heartbeats leases, relays the outbox.

Long-running work (pipeline runs, investigations, recoveries, evaluations) runs here, never in
HTTP requests. If this process dies, leases expire and another worker re-claims the job; all
handlers are idempotent (graph checkpoints, operation ids, unique operation keys).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC

from sqlalchemy import text

from datawarden.db.session import new_session
from datawarden.logs import configure_logging
from datawarden.services import jobs

log = logging.getLogger("datawarden.worker")
HANDLERS: dict[str, Callable[[dict], dict]] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn

    return deco


def _beat(worker_id: str, info: dict) -> None:
    with new_session() as db:
        db.execute(
            text("""INSERT INTO worker_heartbeats (worker_id, seen_at, info) VALUES (:w, now(), CAST(:i AS jsonb))
                           ON CONFLICT (worker_id) DO UPDATE SET seen_at = now(), info = EXCLUDED.info"""),
            {"w": worker_id, "i": __import__("json").dumps(info)},
        )
        db.commit()


def run_one(worker_id: str, kinds: list[str] | None = None) -> dict | None:
    """Claim and execute a single job. Returns the job row or None if nothing was ready."""
    from datawarden.worker import handlers  # noqa: F401 - registers handlers

    with new_session() as db:
        jobs.relay_outbox(db)
        job = jobs.claim(db, worker_id, kinds)
    if job is None:
        return None
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(jobs.LEASE_SECONDS / 3):
            with new_session() as hdb:
                if not jobs.heartbeat(hdb, job["id"], worker_id):
                    log.warning("lost lease on job %s", job["id"])
                    return

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    log.info("job %s (%s) attempt %s", job["id"], job["kind"], job["attempts"], extra={"job_id": job["id"]})
    try:
        fn = HANDLERS.get(job["kind"])
        if fn is None:
            raise RuntimeError(f"no handler for job kind {job['kind']}")
        result = fn({**job["payload"], "_job_id": job["id"], "_worker_id": worker_id})
        stop.set()
        with new_session() as db:
            jobs.complete(db, job["id"], worker_id, result)
        job["status"], job["result"] = "succeeded", result
    except Exception as exc:  # noqa: BLE001
        stop.set()
        log.exception("job %s failed", job["id"], extra={"job_id": job["id"]})
        with new_session() as db:
            jobs.fail(
                db,
                job["id"],
                worker_id,
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}",
                job["attempts"],
                job["max_attempts"],
            )
        job["status"], job["error"] = "failed", str(exc)
        if job["attempts"] >= job["max_attempts"] and job.get("incident_id"):
            _escalate_after_failure(job["incident_id"], job["kind"], exc)
    return job


def _escalate_after_failure(incident_id: str, kind: str, exc: Exception) -> None:
    """A job that exhausted its retries must leave a visible, terminal incident state."""
    from datetime import datetime

    from datawarden.db.models import Incident
    from datawarden.services.audit import audit, publish

    with new_session() as db:
        inc = db.get(Incident, incident_id)
        if inc is None or inc.status in (
            "resolved",
            "closed_no_action",
            "escalated",
            "cancelled",
            "manual_intervention",
        ):
            return
        # a failure while recovering may have left canonical data mid-change: demand a human
        inc.status = "manual_intervention" if inc.status == "recovering" else "escalated"
        inc.terminal_reason = f"{kind} failed after retries: {type(exc).__name__}: {str(exc)[:300]}"
        inc.closed_at = datetime.now(UTC)
        inc.version += 1
        audit(
            db,
            "system:worker",
            f"incident.{inc.status}",
            "incident",
            incident_id,
            incident_id=incident_id,
            detail={"job_kind": kind, "error": str(exc)[:500]},
        )
        publish(db, incident_id, "incident.status", {"status": inc.status, "reason": inc.terminal_reason})
        db.commit()


def drain(
    worker_id: str = "inline", max_jobs: int = 50, kinds: list[str] | None = None, wait_for_delayed: float = 0
) -> list[dict]:
    """Run jobs until none are ready (used by tests and the scripted demo)."""
    done = []
    deadline = time.monotonic() + wait_for_delayed
    while len(done) < max_jobs:
        job = run_one(worker_id, kinds)
        if job is None:
            if time.monotonic() < deadline:
                time.sleep(0.5)
                continue
            break
        done.append(job)
    return done


def main() -> None:
    configure_logging()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    log.info("worker %s started", worker_id)
    last_beat = 0.0
    while not stopping.is_set():
        if time.monotonic() - last_beat > 10:
            try:
                _beat(worker_id, {"pid": os.getpid()})
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")
            last_beat = time.monotonic()
        try:
            job = run_one(worker_id)
        except Exception:  # noqa: BLE001 - keep the worker alive on DB hiccups
            log.exception("worker loop error")
            job = None
            time.sleep(2)
        if job is None:
            stopping.wait(1.0)
    log.info("worker %s stopped", worker_id)
