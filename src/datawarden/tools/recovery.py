"""Recovery tools. Callers are fixed: the Repair Planner may only *propose*; shadow/validation/
approval requests belong to the deterministic controller (the Verification Analyst may request
extra protected checks); execution, canonical verification and rollback belong to the executor
identity only. No agent can reach a tool that writes canonical data.
"""

from __future__ import annotations

from datetime import UTC, date

from pydantic import BaseModel, Field

from datawarden.contracts.agents import RepairProposalDraft
from datawarden.db.models import Incident, RepairProposal
from datawarden.db.session import new_session
from datawarden.recovery import executor, proposals, shadow, validation
from datawarden.tools.base import ToolContext, ToolError, ToolSpec, register


class ProposeArgs(RepairProposalDraft):
    revision: int = Field(ge=1, le=10)


def propose_patch(ctx: ToolContext, args: ProposeArgs) -> dict:
    with new_session() as db:
        incident = db.get(Incident, ctx.incident_id)
        if incident is None:
            raise ToolError("incident not found")
        draft = RepairProposalDraft.model_validate(args.model_dump(exclude={"revision"}))
        prop = proposals.create(db, incident, draft, revision=args.revision, agent=ctx.caller)
        db.commit()
        return {
            "summary": f"proposal {prop.id} ({prop.kind}) hash {prop.patch_hash[:12]} scope {prop.partition_scope}",
            "proposal_id": prop.id,
            "patch_hash": prop.patch_hash,
            "base_commit": prop.base_commit,
            "source_watermark": prop.source_watermark,
            "diff": prop.patch[:6000],
        }


class ProposalRef(BaseModel):
    proposal_id: str = Field(max_length=48)


def _proposal(db, proposal_id: str, incident_id: str | None) -> RepairProposal:
    prop = db.get(RepairProposal, proposal_id)
    if prop is None or (incident_id and prop.incident_id != incident_id):
        raise ToolError("proposal not found for this incident")
    return prop


def create_shadow_environment(ctx: ToolContext, args: ProposalRef) -> dict:
    with new_session() as db:
        prop = _proposal(db, args.proposal_id, ctx.incident_id)
        env = shadow.create(prop.incident_id, prop.revision, proposals.files_of(prop), prop.base_commit)
        prop.shadow_schema = env.schema
        prop.status = "validating"
        db.commit()
    return {
        "summary": f"shadow schema {env.schema}; {len(env.pending_ingested)} pending batch(es) ingested in shadow, "
        f"{len(env.pending_rejected)} rejected",
        "shadow_schema": env.schema,
        "pending_ingested": env.pending_ingested,
        "pending_rejected": env.pending_rejected,
    }


def run_shadow_pipeline(ctx: ToolContext, args: ProposalRef) -> dict:
    with new_session() as db:
        prop = _proposal(db, args.proposal_id, ctx.incident_id)
        if not prop.shadow_schema:
            raise ToolError("create the shadow environment first")
        env = shadow.ShadowEnv(prop.shadow_schema, _code_dir(prop.shadow_schema), _art(prop.shadow_schema), [], {})
        res = shadow.run(env, prop.base_commit[:12])
        tests = shadow.run_tests(env) if res.ok else None
    return {
        "summary": f"shadow build {'succeeded' if res.ok else 'FAILED'}"
        + (f"; dbt tests {'passed' if tests.ok else 'failed'}" if tests else ""),
        "build_ok": res.ok,
        "build_log_tail": res.log_tail[-1500:],
        "tests_ok": tests.ok if tests else None,
        "test_failures": [f["unique_id"].split(".")[2] for f in tests.failures()] if tests else [],
        "tests_log_tail": tests.log_tail[-800:] if tests else "",
    }


def _code_dir(schema: str):
    from datawarden.config import get_settings

    return get_settings().artifact_dir / "shadow" / schema / "code"


def _art(schema: str):
    from datawarden.config import get_settings

    return get_settings().artifact_dir / "shadow" / schema / "dbt"


class ValidateArgs(ProposalRef):
    build_ok: bool = True
    build_detail: str = ""
    tests_ok: bool | None = None
    tests_detail: str = ""
    checks: list[str] | None = Field(default=None, max_length=len(validation.STANDARD_CHECKS))


def run_protected_validation(ctx: ToolContext, args: ValidateArgs) -> dict:
    if args.checks and set(args.checks) - set(validation.STANDARD_CHECKS):
        raise ToolError(f"unknown protected checks: {sorted(set(args.checks) - set(validation.STANDARD_CHECKS))}")
    with new_session() as db:
        prop = _proposal(db, args.proposal_id, ctx.incident_id)
        if not prop.shadow_schema:
            raise ToolError("no shadow environment for this proposal")
        snapshot = {
            "source_watermark": prop.source_watermark,
            "base_commit": prop.base_commit,
            "shadow_schema": prop.shadow_schema,
        }
        results = validation.run_validation(
            proposal_hash=prop.patch_hash,
            kind=prop.kind,
            files=proposals.files_of(prop),
            partition_scope=prop.partition_scope,
            shadow_schema=prop.shadow_schema,
            code_dir=_code_dir(prop.shadow_schema),
            input_snapshot=snapshot,
            build_ok=args.build_ok,
            build_detail=args.build_detail,
            tests_ok=args.tests_ok,
            tests_detail=args.tests_detail,
            only=args.checks,
        )
        from datawarden.db.models import Validation

        for r in results:
            db.add(
                Validation(
                    proposal_id=prop.id,
                    proposal_hash=r.proposal_hash,
                    input_snapshot=r.input_snapshot,
                    check_id=r.check_id,
                    expected=r.expected,
                    observed=r.observed,
                    status=r.status,
                    artifact_ref=f"shadow:{prop.shadow_schema}",
                )
            )
        failed = [r.check_id for r in results if r.status != "pass"]
        prop.status = "validation_failed" if failed else "validated"
        db.commit()
    return {
        "summary": f"{len(results)} protected check(s); failed: {failed or 'none'}",
        "results": [r.model_dump() for r in results],
        "failed": failed,
    }


class ApprovalReq(ProposalRef):
    pass


def request_approval(ctx: ToolContext, args: ApprovalReq) -> dict:
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from datawarden.config import get_settings
    from datawarden.db.models import Approval
    from datawarden.services.audit import audit, publish

    with new_session() as db:
        prop = _proposal(db, args.proposal_id, ctx.incident_id)
        if prop.status != "validated":
            raise ToolError(f"proposal is {prop.status}; only validated proposals can request approval")
        appr = db.scalar(select(Approval).where(Approval.proposal_id == prop.id))
        if appr is None:
            appr = Approval(
                incident_id=prop.incident_id,
                proposal_id=prop.id,
                proposal_hash=prop.patch_hash,
                base_commit=prop.base_commit,
                source_watermark=prop.source_watermark,
                status="pending",
                expires_at=datetime.now(UTC) + timedelta(hours=get_settings().approval_expiry_hours),
            )
            db.add(appr)
            prop.status = "awaiting_approval"
            db.flush()
            audit(
                db,
                "system:controller",
                "approval.requested",
                "approval",
                appr.id,
                incident_id=prop.incident_id,
                detail={"proposal_id": prop.id, "proposal_hash": prop.patch_hash},
            )
            publish(db, prop.incident_id, "approval.requested", {"approval_id": appr.id, "proposal_id": prop.id})
        db.commit()
        return {
            "summary": f"approval {appr.id} {appr.status}, expires {appr.expires_at.isoformat()}",
            "approval_id": appr.id,
            "status": appr.status,
            "version": appr.version,
        }


class ExecArgs(BaseModel):
    approval_id: str = Field(max_length=40)


def execute_approved_repair(ctx: ToolContext, args: ExecArgs) -> dict:
    out = executor.execute(args.approval_id)
    return {"summary": f"recovery {out.get('status')}" + (f": {out.get('error')}" if out.get("error") else ""), **out}


class VerifyArgs(BaseModel):
    operation_id: str = Field(max_length=40)
    partitions: list[date] = Field(min_length=1, max_length=62)


def verify_canonical_outputs(ctx: ToolContext, args: VerifyArgs) -> dict:
    from datawarden.db.models import RecoveryOperation

    with new_session() as db:
        op = db.get(RecoveryOperation, args.operation_id)
        if op is None:
            raise ToolError("operation not found")
        snap = next((j for j in op.journal if j["step"] == "snapshot"), None)
    ok, detail = executor.verify_canonical(
        args.operation_id, sorted(args.partitions), snap["fingerprints"] if snap else {}
    )
    return {"summary": f"canonical verification {'passed' if ok else 'failed'}", "ok": ok, **detail}


def rollback_repair(ctx: ToolContext, args: VerifyArgs) -> dict:
    from datawarden.db.models import RecoveryOperation

    with new_session() as db:
        op = db.get(RecoveryOperation, args.operation_id)
        if op is None:
            raise ToolError("operation not found")
        snap = next((j for j in op.journal if j["step"] == "snapshot"), None)
        ok, detail = executor.rollback(db, op, sorted(args.partitions), snap["fingerprints"] if snap else {})
    return {"summary": f"rollback {'verified' if ok else 'FAILED - manual intervention required'}", "ok": ok, **detail}


CONTROLLER = frozenset({"controller"})
EXECUTOR = frozenset({"executor"})
for _spec in (
    ToolSpec(
        "propose_patch",
        "Persist a repair proposal draft (cannot apply or approve).",
        ProposeArgs,
        propose_patch,
        frozenset({"repair_planner", "single_agent"}),
        counts_toward_budget=False,
    ),
    ToolSpec(
        "create_shadow_environment",
        "Create an isolated shadow schema + patched code copy.",
        ProposalRef,
        create_shadow_environment,
        CONTROLLER,
        timeout_s=180,
        counts_toward_budget=False,
    ),
    ToolSpec(
        "run_shadow_pipeline",
        "Build all models in the shadow schema and run dbt tests.",
        ProposalRef,
        run_shadow_pipeline,
        CONTROLLER,
        timeout_s=600,
        counts_toward_budget=False,
    ),
    ToolSpec(
        "run_protected_validation",
        "Run protected checks against a shadow build.",
        ValidateArgs,
        run_protected_validation,
        CONTROLLER | {"verification_analyst"},
        timeout_s=300,
        counts_toward_budget=False,
        max_output_bytes=40_000,
    ),
    ToolSpec(
        "request_approval",
        "Open a pending approval for a validated proposal.",
        ApprovalReq,
        request_approval,
        CONTROLLER,
        counts_toward_budget=False,
    ),
    ToolSpec(
        "execute_approved_repair",
        "Execute an approved repair (executor identity only).",
        ExecArgs,
        execute_approved_repair,
        EXECUTOR,
        timeout_s=1200,
        mutating=True,
        counts_toward_budget=False,
    ),
    ToolSpec(
        "verify_canonical_outputs",
        "Verify canonical outputs after a repair (executor only).",
        VerifyArgs,
        verify_canonical_outputs,
        EXECUTOR,
        timeout_s=300,
        counts_toward_budget=False,
    ),
    ToolSpec(
        "rollback_repair",
        "Restore code and partitions from snapshot (executor only).",
        VerifyArgs,
        rollback_repair,
        EXECUTOR,
        timeout_s=900,
        mutating=True,
        counts_toward_budget=False,
    ),
):
    register(_spec)
