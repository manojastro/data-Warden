"""Approval decisions with optimistic concurrency, expiry, and idempotent replay."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from datawarden.db.models import Approval, Incident, RepairProposal
from datawarden.services import jobs
from datawarden.services.audit import audit, publish


class ApprovalConflict(Exception):
    pass


def expire_if_due(db: Session, approval: Approval) -> bool:
    if approval.status == "pending" and approval.expires_at < datetime.now(UTC):
        db.execute(
            text(
                "UPDATE approvals SET status='expired', version=version+1, "
                "invalidation_reason='approval window elapsed' WHERE id=:id AND status='pending'"
            ),
            {"id": approval.id},
        )
        db.execute(text("UPDATE repair_proposals SET status='expired' WHERE id=:id"), {"id": approval.proposal_id})
        publish(db, approval.incident_id, "approval.expired", {"approval_id": approval.id})
        db.flush()
        db.refresh(approval)
        return True
    return False


def decide(
    db: Session,
    approval_id: str,
    decision: str,
    expected_version: int,
    *,
    user_id: str,
    actor: str,
    comment: str | None,
    request_id: str | None,
) -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    approval = db.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise LookupError("approval not found")
    target = "approved" if decision == "approve" else "rejected"
    # Idempotent replay: the same decision by the same user returns the stored outcome.
    if approval.status == target and approval.decided_by == user_id:
        return {"approval_id": approval.id, "status": approval.status, "version": approval.version, "replayed": True}
    if expire_if_due(db, approval):
        raise ApprovalConflict("approval expired")
    if approval.status != "pending":
        raise ApprovalConflict(f"approval is {approval.status}")
    if approval.version != expected_version:
        raise ApprovalConflict(f"stale version {expected_version}; current is {approval.version}")
    proposal = db.get(RepairProposal, approval.proposal_id)
    if proposal is None or proposal.patch_hash != approval.proposal_hash:
        raise ApprovalConflict("proposal changed since approval was requested")
    res = db.execute(
        text("""UPDATE approvals SET status=:s, decided_by=:u, decided_at=now(), comment=:c,
                             version=version+1 WHERE id=:id AND version=:v AND status='pending'"""),
        {"s": target, "u": user_id, "c": (comment or "")[:1000], "id": approval_id, "v": expected_version},
    )
    if res.rowcount != 1:
        raise ApprovalConflict("concurrent decision")
    proposal.status = "approved" if target == "approved" else "rejected"
    incident = db.get(Incident, approval.incident_id)
    incident.version += 1
    audit(
        db,
        actor,
        f"approval.{target}",
        "approval",
        approval.id,
        incident_id=approval.incident_id,
        detail={"proposal_id": proposal.id, "proposal_hash": proposal.patch_hash, "comment": comment},
        request_id=request_id,
    )
    publish(db, approval.incident_id, "approval.decided", {"approval_id": approval.id, "decision": target, "by": actor})
    jobs.outbox(
        db, "approval.decided", {"incident_id": approval.incident_id, "approval_id": approval.id, "decision": target}
    )
    db.flush()
    db.refresh(approval)
    return {"approval_id": approval.id, "status": approval.status, "version": approval.version, "replayed": False}
