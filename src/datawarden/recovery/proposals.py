"""Persisting repair proposals with an immutable content hash and a reviewable diff."""

from __future__ import annotations

import difflib
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from datawarden import sources, workspace
from datawarden.contracts.agents import RepairProposalDraft
from datawarden.db.models import Incident, RepairProposal


def proposal_hash(
    kind: str,
    files: dict[str, str],
    asset_scope: list[str],
    partition_scope: list[str],
    base_commit: str,
    source_watermark: int,
) -> str:
    """Any change to the patch, scope, code base, or source snapshot changes the hash."""
    payload = {
        "kind": kind,
        "files": {k: files[k] for k in sorted(files)},
        "assets": sorted(set(asset_scope)),
        "partitions": sorted(set(partition_scope)),
        "base_commit": base_commit,
        "source_watermark": source_watermark,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def render_diff(files: dict[str, str], base_commit: str) -> str:
    out = []
    for rel in sorted(files):
        try:
            old = workspace.read_file(rel, commit=base_commit)
        except workspace.WorkspaceError:
            old = ""
        out.extend(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                files[rel].splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
    return "".join(out)


def create(db: Session, incident: Incident, draft: RepairProposalDraft, *, revision: int, agent: str) -> RepairProposal:
    """Idempotent on (incident, revision): re-running the node after a crash returns the same row."""
    existing = db.scalar(
        select(RepairProposal).where(RepairProposal.incident_id == incident.id, RepairProposal.revision == revision)
    )
    if existing:
        return existing
    base = workspace.head_commit()
    watermark = sources.source_watermark()
    phash = proposal_hash(draft.kind, draft.files, draft.asset_scope, draft.partition_scope, base, watermark)
    prop = RepairProposal(
        id=f"P-{incident.id[-6:]}-r{revision}",
        incident_id=incident.id,
        revision=revision,
        kind=draft.kind,
        summary=draft.summary,
        patch=render_diff(draft.files, base) if draft.files else "",
        patch_hash=phash,
        base_commit=base,
        source_watermark=watermark,
        asset_scope=sorted(set(draft.asset_scope)),
        partition_scope=sorted(set(draft.partition_scope)),
        preconditions=draft.preconditions,
        risks=draft.risks,
        rollback_plan={"plan": draft.rollback_plan, "files": sorted(draft.files)},
        claimed_confidence=draft.claimed_confidence,
        status="proposed",
        policy_result={"files": draft.files, "created_by": agent},
    )
    db.add(prop)
    db.flush()
    return prop


def files_of(prop: RepairProposal) -> dict[str, str]:
    return dict(prop.policy_result.get("files", {}))


def recompute_hash(prop: RepairProposal) -> str:
    return proposal_hash(
        prop.kind, files_of(prop), prop.asset_scope, prop.partition_scope, prop.base_commit, prop.source_watermark
    )
