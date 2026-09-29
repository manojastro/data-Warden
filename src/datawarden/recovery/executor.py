"""Approval-bound repair executor (saga with an operation journal).

git, the warehouse, and the application database do not share a transaction, so execution is an
explicit sequence of journaled, individually idempotent steps:

  recheck -> lock partitions -> snapshot -> promote code -> apply (bounded replay)
          -> verify canonical outputs -> release  |  on failure: rollback -> verify rollback

``operation_key = proposal_hash:approval_id`` is unique, so duplicate approvals, duplicate job
deliveries, or a worker crash and retry can never commit more than one logical repair. A re-run
reads the journal and resumes after the last completed step.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta

from psycopg import sql
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from datawarden import sources, workspace
from datawarden.checks.registry import run_checks, store_results
from datawarden.config import get_settings
from datawarden.db.models import (
    Approval,
    Artifact,
    Incident,
    PartitionLock,
    RecoveryOperation,
    RepairProposal,
    User,
    Validation,
)
from datawarden.db.session import new_session
from datawarden.oracle import reconciliation
from datawarden.pipeline.runner import run_pipeline
from datawarden.recovery.proposals import files_of, recompute_hash
from datawarden.services import auth
from datawarden.services.audit import audit, publish
from datawarden.warehouse.conn import connect

log = logging.getLogger(__name__)
MART = "mart_daily_revenue"
MART_COLS = (
    "business_date",
    "gross_collected_paise",
    "refunds_paise",
    "net_revenue_paise",
    "captured_payment_count",
    "refund_count",
)
SNAPSHOT_RETENTION_DAYS = 30
TERMINAL = ("succeeded", "rolled_back", "manual_intervention", "invalidated", "conflict")


def _journal(db, op: RecoveryOperation, step: str, status: str = "done", **detail) -> None:
    op.journal = [
        *op.journal,
        {
            "step": step,
            "status": status,
            "at": datetime.now(UTC).isoformat(),
            **json.loads(json.dumps(detail, default=str)),
        },
    ]
    db.commit()
    publish(db, op.incident_id, "recovery.step", {"operation_id": op.id, "step": step, "status": status})
    db.commit()


def _done(op: RecoveryOperation, step: str) -> dict | None:
    return next((j for j in op.journal if j["step"] == step and j["status"] == "done"), None)


def recheck(db, approval: Approval, proposal: RepairProposal) -> list[str]:
    """Pre-mutation checks binding execution to the immutable approval record."""
    problems = []
    if approval.status != "approved":
        problems.append(f"approval is {approval.status}")
    if approval.proposal_hash != proposal.patch_hash or recompute_hash(proposal) != proposal.patch_hash:
        problems.append("proposal content no longer matches the approved hash")
    user = db.get(User, approval.decided_by) if approval.decided_by else None
    if user is None or not auth.allowed(user.role_id, "approval.decide"):
        problems.append("approval was not made by an authorized approver")
    if approval.decided_at and approval.decided_at > approval.expires_at:
        problems.append("approval decided after expiry")
    if sources.source_watermark() != proposal.source_watermark:
        problems.append(
            f"source snapshot is stale (watermark {proposal.source_watermark} -> {sources.source_watermark()})"
        )
    if workspace.head_commit() != proposal.base_commit:
        problems.append("pipeline code changed since validation")
    vals = list(
        db.scalars(
            select(Validation).where(
                Validation.proposal_id == proposal.id, Validation.proposal_hash == proposal.patch_hash
            )
        )
    )
    if not vals or any(v.status != "pass" for v in vals):
        problems.append("no passing shadow validation bound to this proposal hash")
    return problems


def _mart_rows(conn, dates: list[date] | None = None) -> list[dict]:
    q = f"SELECT {', '.join(MART_COLS)} FROM marts.{MART}"
    if dates is not None:
        return [dict(r) for r in conn.execute(q + " WHERE business_date = ANY(%s) ORDER BY 1", (dates,)).fetchall()]
    return [dict(r) for r in conn.execute(q + " ORDER BY 1").fetchall()]


def _fingerprint(rows: list[dict]) -> dict[str, str]:
    return {
        str(r["business_date"]): hashlib.sha256(
            json.dumps([r[c] for c in MART_COLS], default=str).encode()
        ).hexdigest()[:16]
        for r in rows
    }


def _snapshot_checksum(conn, table: sql.Composable) -> str:
    row = conn.execute(
        sql.SQL("SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY business_date), '')) AS h FROM {} t").format(
            table
        )
    ).fetchone()
    return row["h"]


def execute(approval_id: str) -> dict:
    with new_session() as db:
        approval = db.get(Approval, approval_id)
        if approval is None:
            return {"status": "error", "error": "approval not found"}
        proposal = db.get(RepairProposal, approval.proposal_id)
        key = f"{approval.proposal_hash}:{approval.id}"
        db.execute(
            insert(RecoveryOperation)
            .values(
                id=f"rop_{hashlib.sha256(key.encode()).hexdigest()[:16]}",
                operation_key=key,
                incident_id=approval.incident_id,
                proposal_id=proposal.id,
                approval_id=approval.id,
                status="started",
                journal=[],
            )
            .on_conflict_do_nothing(index_elements=["operation_key"])
        )
        db.commit()
        op = db.scalar(select(RecoveryOperation).where(RecoveryOperation.operation_key == key).with_for_update())
        if op.status in TERMINAL:
            db.commit()
            return {"status": op.status, "operation_id": op.id, "replayed": True, "error": op.error}
        db.commit()
        return _run(db, op, approval, proposal)


def _run(db, op: RecoveryOperation, approval: Approval, proposal: RepairProposal) -> dict:
    s = get_settings()
    incident = db.get(Incident, op.incident_id)
    scope = sorted(date.fromisoformat(d) for d in proposal.partition_scope)
    files = files_of(proposal)

    # 1. recheck (skipped on resume once passed: later steps legitimately change HEAD)
    if not _done(op, "recheck"):
        problems = recheck(db, approval, proposal)
        if problems:
            approval.status = "invalidated"
            approval.invalidation_reason = "; ".join(problems)[:1000]
            approval.version += 1
            proposal.status = "invalidated"
            op.status, op.error = "invalidated", approval.invalidation_reason
            op.finished_at = datetime.now(UTC)
            audit(
                db,
                "system:executor",
                "approval.invalidated",
                "approval",
                approval.id,
                incident_id=op.incident_id,
                detail={"problems": problems},
            )
            _journal(db, op, "recheck", "failed", problems=problems)
            return {"status": "invalidated", "operation_id": op.id, "problems": problems}
        _journal(db, op, "recheck", base_commit=proposal.base_commit, source_watermark=proposal.source_watermark)

    # 2. locks: every scoped mart partition + every asset whose code changes
    if not _done(op, "lock"):
        keys = [f"{MART}:{d}" for d in scope] + [f"asset:{f.split('/')[-1].removesuffix('.sql')}" for f in files]
        try:
            for k in keys:
                db.add(PartitionLock(lock_key=k, operation_id=op.id))
            db.flush()
        except IntegrityError:
            db.rollback()
            holders = [
                pl.operation_id for pl in db.scalars(select(PartitionLock).where(PartitionLock.lock_key.in_(keys)))
            ]
            op = db.get(RecoveryOperation, op.id)
            op.status, op.error = "conflict", f"partitions locked by {sorted(set(holders))}"
            op.finished_at = datetime.now(UTC)
            _journal(db, op, "lock", "failed", holders=holders)
            return {"status": "conflict", "operation_id": op.id, "holders": holders}
        op.lock_keys = keys
        _journal(db, op, "lock", keys=keys)

    snap = sql.Identifier("recovery", f"snap_{op.id}")
    # 3. snapshot scoped partitions (+ fingerprints of all partitions for boundary verification)
    if not _done(op, "snapshot"):
        with connect("executor") as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(snap))
            conn.execute(
                sql.SQL("CREATE TABLE {} AS SELECT * FROM marts.{} WHERE business_date = ANY(%s)").format(
                    snap, sql.Identifier(MART)
                ),
                (scope,),
            )
            checksum = _snapshot_checksum(conn, snap)
            all_rows = _mart_rows(conn)
            conn.commit()
        pre_diffs = [
            (str(d.business_date), d.field, d.expected, d.observed)
            for d in reconciliation.compare_daily(all_rows, set(scope))
        ]
        op.snapshot_ref = f"warehouse:recovery.snap_{op.id}"
        op.snapshot_checksum = checksum
        op.pre_commit = workspace.head_commit()
        db.add(
            Artifact(
                kind="rollback_snapshot",
                path=op.snapshot_ref,
                sha256=checksum,
                size_bytes=0,
                incident_id=op.incident_id,
                retention_until=datetime.now(UTC) + timedelta(days=SNAPSHOT_RETENTION_DAYS),
            )
        )
        _journal(
            db,
            op,
            "snapshot",
            checksum=checksum,
            fingerprints=_fingerprint(all_rows),
            pre_diffs=pre_diffs,
            retention_days=SNAPSHOT_RETENTION_DAYS,
        )
    fingerprints = _done(op, "snapshot")["fingerprints"]

    try:
        # 4. promote code through a versioned commit tagged with the operation id
        if files and not _done(op, "promote"):
            head = workspace.log(1)[0]
            if f"[op:{op.id}]" in head["subject"]:
                post = head["commit"]
            else:
                post = workspace.commit_files(
                    files,
                    f"repair INC-{incident.number}: {proposal.summary[:60]} "
                    f"[proposal {proposal.id} {proposal.patch_hash[:8]}] [op:{op.id}]",
                )
            op.post_commit = post
            _journal(db, op, "promote", post_commit=post)
        # 5. apply: ingest pending batches, rebuild upstream, recompute exactly the scoped partitions
        if not _done(op, "apply"):
            report = run_pipeline(
                trigger="executor",
                dbt_target="executor",
                replay_dates=scope,
                run_id=f"exec_{op.id[-12:]}_{len(op.journal)}",
                run_checks_after=False,
            )
            if report.status != "success":
                raise RuntimeError(f"apply failed: {report.error or report.tasks}")
            _journal(db, op, "apply", run_id=report.run_id)
            if s.chaos_enabled("postcheck_corrupt"):
                with connect("executor") as conn:  # demo-only: simulate a partial/corrupt write
                    conn.execute(
                        f"UPDATE marts.{MART} SET net_revenue_paise = net_revenue_paise + 100 WHERE business_date = %s",
                        (scope[0],),
                    )
                    conn.commit()
        # 6. verify canonical outputs
        ok, detail = verify_canonical(op.id, scope, fingerprints)
        _journal(db, op, "verify", "done" if ok else "failed", **detail)
        if not ok:
            raise RuntimeError("canonical verification failed: " + "; ".join(detail["problems"])[:500])
    except Exception as exc:  # noqa: BLE001 - every failure path goes through verified rollback
        log.warning("repair %s failed, rolling back: %s", op.id, exc)
        op.error = str(exc)[:2000]
        op.status = "rolling_back"
        db.commit()
        rb_ok, rb = rollback(db, op, scope, fingerprints)
        op.status = "rolled_back" if rb_ok else "manual_intervention"
        op.finished_at = datetime.now(UTC)
        proposal.status = "rolled_back" if rb_ok else "failed"
        _release(db, op)
        _journal(db, op, "rollback", "done" if rb_ok else "failed", **rb)
        audit(
            db,
            "system:executor",
            f"recovery.{op.status}",
            "recovery_operation",
            op.id,
            incident_id=op.incident_id,
            detail={"error": op.error, "rollback": rb},
        )
        db.commit()
        return {"status": op.status, "operation_id": op.id, "error": op.error, "rollback": rb}

    op.status = "succeeded"
    op.finished_at = datetime.now(UTC)
    proposal.status = "applied"
    _release(db, op)
    audit(
        db,
        "system:executor",
        "recovery.succeeded",
        "recovery_operation",
        op.id,
        incident_id=op.incident_id,
        detail={"post_commit": op.post_commit, "partitions": [str(d) for d in scope]},
    )
    _journal(db, op, "complete")
    return {"status": "succeeded", "operation_id": op.id, "post_commit": op.post_commit}


def _release(db, op: RecoveryOperation) -> None:
    db.execute(delete(PartitionLock).where(PartitionLock.operation_id == op.id))
    db.commit()


def verify_canonical(op_id: str, scope: list[date], fingerprints: dict[str, str]) -> tuple[bool, dict]:
    problems = []
    with connect("validator") as conn:
        rows = _mart_rows(conn)
        results = run_checks(conn)
    in_scope = [r for r in rows if r["business_date"] in scope]
    diffs = reconciliation.compare_daily(in_scope, set(scope))
    if diffs:
        problems.append(
            f"scoped partitions still disagree with source truth: {sorted({str(d.business_date) for d in diffs})}"
        )
    now = _fingerprint(rows)
    moved = sorted(
        d
        for d in set(now) | set(fingerprints)
        if date.fromisoformat(d) not in scope and now.get(d) != fingerprints.get(d)
    )
    if moved:
        problems.append(f"partitions outside the approved scope changed: {moved}")
    failing = [r.check_id for r in results if r.status in ("fail", "error")]
    if failing:
        problems.append(f"canonical checks failing after repair: {failing}")
    with connect("pipeline", autocommit=True) as pconn:
        store_results(pconn, results, None)
    return not problems, {
        "problems": problems,
        "checks_failing": failing,
        "outside_scope_changed": moved,
        "checks_run": len(results),
    }


def rollback(db, op: RecoveryOperation, scope: list[date], fingerprints: dict[str, str]) -> tuple[bool, dict]:
    """Restore previous code (history-preserving revert) and the scoped partitions from snapshot; verify."""
    detail: dict = {}
    try:
        if op.pre_commit and workspace.head_commit() != op.pre_commit:
            detail["revert_commit"] = workspace.revert_to(
                op.pre_commit, f"rollback [op:{op.id}]: restore {op.pre_commit[:10]}"
            )
            report = run_pipeline(
                trigger="rollback",
                dbt_target="executor",
                run_checks_after=False,
                run_id=f"rb_{op.id[-12:]}",
                replay_dates=scope,
            )
            if report.status != "success":
                raise RuntimeError(f"upstream rebuild failed during rollback: {report.error}")
        snap = sql.Identifier("recovery", f"snap_{op.id}")
        with connect("executor") as conn:
            with conn.transaction():
                conn.execute(
                    sql.SQL("DELETE FROM marts.{} WHERE business_date = ANY(%s)").format(sql.Identifier(MART)), (scope,)
                )
                conn.execute(sql.SQL("INSERT INTO marts.{} SELECT * FROM {}").format(sql.Identifier(MART), snap))
            restored = conn.execute(
                sql.SQL("SELECT * FROM marts.{} WHERE business_date = ANY(%s)").format(sql.Identifier(MART)), (scope,)
            ).fetchall()
            conn.execute(
                "CREATE TEMP TABLE _restored AS SELECT * FROM marts." + MART + " WHERE business_date = ANY(%s)",
                (scope,),
            )
            restored_sum = _snapshot_checksum(conn, sql.Identifier("_restored"))
            all_rows = _mart_rows(conn)
            conn.rollback()
        detail["restored_partitions"] = len(restored)
        detail["checksum_match"] = restored_sum == op.snapshot_checksum
        now = _fingerprint(all_rows)
        detail["fingerprints_match"] = all(now.get(d) == f for d, f in fingerprints.items()) and set(now) == set(
            fingerprints
        )
        pre = _done(op, "snapshot")["pre_diffs"]
        post = [
            [str(d.business_date), d.field, d.expected, d.observed]
            for d in reconciliation.compare_daily(all_rows, set(scope))
        ]
        detail["oracle_state_matches_pre_change"] = sorted(map(list, pre)) == sorted(post)
        ok = detail["checksum_match"] and detail["fingerprints_match"] and detail["oracle_state_matches_pre_change"]
        return ok, detail
    except Exception as exc:  # noqa: BLE001
        detail["error"] = str(exc)[:1000]
        return False, detail
