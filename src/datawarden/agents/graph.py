"""Incident execution graph (LangGraph) with Postgres checkpoints.

This is the *execution* graph of the investigation, not the data lineage graph. Routing,
budgets, policy, approval and permission enforcement are deterministic code here; agents only
produce structured findings and proposals.

    intake -> load_context -> {quality_investigator || lineage_investigator} -> root_cause
    root_cause -> root_cause (next round) | repair_planner | close_no_action | escalate
    repair_planner -> policy_check | escalate
    policy_check -> shadow_validate | repair_planner (feedback) | escalate
    shadow_validate -> verification
    verification -> request_approval | repair_planner (feedback) | escalate
    request_approval --(interrupt: human decision)--> execute | escalate
    execute -> resolve | repair_planner (approval invalidated) | escalate | manual_intervention
"""

from __future__ import annotations

import operator
import time
from datetime import UTC, datetime
from functools import wraps
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import func, select, text

from datawarden import sources, workspace
from datawarden.agents.specialists import run_agent
from datawarden.config import get_settings
from datawarden.contracts.agents import AgentFinding, RepairProposalDraft, VerificationVerdict
from datawarden.db.models import Approval, Evidence, Incident, IncidentEvent, RepairProposal
from datawarden.db.session import new_session
from datawarden.recovery import policy
from datawarden.recovery.validation import STANDARD_CHECKS
from datawarden.services.audit import audit, publish
from datawarden.tools import base as tools

GRAPH_VERSION = "incident-graph/1.0"


def graph_version(variant: str) -> str:
    return GRAPH_VERSION if variant == "multi" else f"{GRAPH_VERSION}+{variant}"


NODES = [
    "intake",
    "load_context",
    "quality_investigator",
    "lineage_investigator",
    "root_cause",
    "repair_planner",
    "policy_check",
    "shadow_validate",
    "verification",
    "request_approval",
    "execute",
    "resolve",
    "close_no_action",
    "escalate",
    "manual_intervention",
]
EDGES = [
    ("intake", "load_context"),
    ("load_context", "quality_investigator"),
    ("load_context", "lineage_investigator"),
    ("quality_investigator", "root_cause"),
    ("lineage_investigator", "root_cause"),
    ("root_cause", "root_cause"),
    ("root_cause", "repair_planner"),
    ("root_cause", "close_no_action"),
    ("root_cause", "escalate"),
    ("repair_planner", "policy_check"),
    ("repair_planner", "escalate"),
    ("policy_check", "shadow_validate"),
    ("policy_check", "repair_planner"),
    ("policy_check", "escalate"),
    ("shadow_validate", "verification"),
    ("verification", "request_approval"),
    ("verification", "repair_planner"),
    ("verification", "escalate"),
    ("request_approval", "execute"),
    ("request_approval", "repair_planner"),
    ("request_approval", "escalate"),
    ("execute", "resolve"),
    ("execute", "repair_planner"),
    ("execute", "escalate"),
    ("execute", "manual_intervention"),
]


def _merge_usage(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + v
    return out


class IncidentState(TypedDict, total=False):
    incident_id: str
    run_id: str | None
    event_ids: list[str]
    asset_ids: list[str]
    severity: str
    affected_partitions: list[str]
    failing_checks: list[dict]
    input_snapshot: dict
    source_watermark: int
    hypotheses: Annotated[list[dict], operator.add]
    evidence_refs: Annotated[list[str], operator.add]
    agent_findings: Annotated[list[dict], operator.add]
    investigation_round: int
    repair_attempt: int
    tool_call_count: int
    elapsed_time: Annotated[float, operator.add]
    token_usage: Annotated[dict, _merge_usage]
    estimated_cost: float | None
    root_cause: dict | None
    proposal_id: str | None
    proposal_hash: str | None
    draft: dict | None
    feedback: list[dict]
    validation_results: list[dict]
    approval_id: str | None
    approval_status: str | None
    recovery: dict | None
    rollback_ref: str | None
    last_error: str | None
    graph_version: str
    route: str | None
    terminal_reason: str | None


def _status(incident_id: str) -> str | None:
    with new_session() as db:
        return db.scalar(select(Incident.status).where(Incident.id == incident_id))


def _set_status(incident_id: str, status: str, **fields: Any) -> None:
    with new_session() as db:
        inc = db.get(Incident, incident_id)
        inc.status = status
        for k, v in fields.items():
            setattr(inc, k, v)
        inc.version += 1
        publish(db, incident_id, "incident.status", {"status": status})
        db.commit()


def node(name: str):
    """Wrap a node: cancellation check, live node-status events, active-time accounting."""

    def deco(fn):
        @wraps(fn)
        def wrapper(state: IncidentState) -> dict:
            iid = state["incident_id"]
            if name != "intake" and _status(iid) == "cancelled":
                if name in ("quality_investigator", "lineage_investigator"):  # parallel: no shared-key writes
                    return {"elapsed_time": 0.0}
                return {"route": "cancelled", "terminal_reason": "cancelled"}
            with new_session() as db:
                publish(db, iid, "node.started", {"node": name})
                db.commit()
            started = time.monotonic()
            try:
                out = fn(state) or {}
            except Exception as exc:
                with new_session() as db:
                    publish(db, iid, "node.failed", {"node": name, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
                    db.commit()
                raise
            dt = time.monotonic() - started
            out["elapsed_time"] = dt if name != "request_approval" else 0.0  # approval wait is excluded
            with new_session() as db:
                publish(db, iid, "node.completed", {"node": name, "seconds": round(dt, 2), "route": out.get("route")})
                db.commit()
            return out

        return wrapper

    return deco


def _deadline(state: IncidentState) -> float:
    remaining = get_settings().budget_active_seconds - state.get("elapsed_time", 0.0)
    return time.monotonic() + max(0.0, remaining)


def _tool_calls(incident_id: str) -> int:
    with new_session() as db:
        usage = db.scalar(select(Incident.usage).where(Incident.id == incident_id)) or {}
    return int(usage.get("tool_calls", 0))


def _usage(info: dict) -> dict:
    return {"input_tokens": info.get("input_tokens", 0), "output_tokens": info.get("output_tokens", 0)}


# --- nodes ------------------------------------------------------------------------------------


@node("intake")
def intake(state: IncidentState) -> dict:
    iid = state["incident_id"]
    with new_session() as db:
        # lease: only one investigation per incident; a retry of the same thread may continue
        res = db.execute(
            text("""UPDATE incidents SET status = 'investigating', graph_thread_id = :t,
                                 graph_version = coalesce(graph_version, :v), model_mode = :m, version = version + 1
                                 WHERE id = :id AND (status = 'open' OR (status = 'investigating' AND graph_thread_id = :t))
                                 RETURNING id"""),
            {
                "id": iid,
                "t": iid,
                "v": GRAPH_VERSION,
                "m": "fixture" if get_settings().model_provider == "fixture" else "live",
            },
        )
        if res.first() is None:
            db.commit()
            return {"route": "skip", "terminal_reason": "duplicate delivery or incident not open"}
        inc = db.get(Incident, iid)
        events = list(db.scalars(select(IncidentEvent).where(IncidentEvent.incident_id == iid)))
        audit(
            db,
            "system:controller",
            "incident.investigation_started",
            "incident",
            iid,
            incident_id=iid,
            detail={"graph_version": GRAPH_VERSION, "events": len(events)},
        )
        publish(db, iid, "incident.status", {"status": "investigating"})
        db.commit()
        return {
            "route": "ok",
            "run_id": inc.run_id,
            "event_ids": [e.id for e in events],
            "asset_ids": inc.asset_ids,
            "severity": inc.severity,
            "graph_version": GRAPH_VERSION,
            "investigation_round": 0,
            "repair_attempt": 0,
            "feedback": [],
        }


@node("load_context")
def load_context(state: IncidentState) -> dict:
    iid = state["incident_id"]
    with new_session() as db:
        inc = db.get(Incident, iid)
        events = list(
            db.scalars(
                select(IncidentEvent)
                .where(IncidentEvent.incident_id == iid)
                .order_by(IncidentEvent.received_at)
                .limit(40)
            )
        )
    failing = [
        {
            "check_id": e.check_id,
            "severity": e.severity,
            "asset": e.asset_id,
            "message": (e.payload.get("message") or "")[:300],
        }
        for e in events
        if e.check_id
    ][:20]
    snapshot = {
        "source_watermark": sources.source_watermark(),
        "code_commit": workspace.head_commit(),
        "run_id": inc.run_id,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    return {
        "failing_checks": failing,
        "affected_partitions": sorted(inc.affected_partitions or []),
        "input_snapshot": snapshot,
        "source_watermark": snapshot["source_watermark"],
    }


def _base_ctx(state: IncidentState) -> dict:
    return {
        "incident_id": state["incident_id"],
        "severity": state.get("severity"),
        "asset_ids": state.get("asset_ids", []),
        "failing_checks": state.get("failing_checks", []),
        "affected_partitions": state.get("affected_partitions", []),
        "run_id": state.get("run_id"),
    }


def _investigator(agent: str):
    @node(agent)
    def run(state: IncidentState) -> dict:
        finding, info = run_agent(agent, state["incident_id"], _base_ctx(state), deadline=_deadline(state))
        f = finding.model_dump(mode="json")
        return {
            "agent_findings": [{"agent": agent, "round": 1, **f}],
            "evidence_refs": f["evidence_ids"],
            "hypotheses": f["hypotheses"],
            "token_usage": _usage(info),
        }

    return run


quality_investigator = _investigator("quality_investigator")
lineage_investigator = _investigator("lineage_investigator")


@node("root_cause")
def root_cause(state: IncidentState) -> dict:
    rnd = state.get("investigation_round", 0) + 1
    prior = {}
    for f in state.get("agent_findings", []):
        prior[f"{f['agent']}#{f.get('round', 1)}"] = {
            k: f.get(k)
            for k in (
                "summary",
                "hypotheses",
                "evidence_ids",
                "affected_partitions",
                "symptoms",
                "next_query",
                "root_cause",
            )
        }
    ctx = {**_base_ctx(state), "prior_findings": prior, "investigation_round": rnd}
    quality = next((f for f in state.get("agent_findings", []) if f["agent"] == "quality_investigator"), None)
    if quality and quality.get("affected_partitions"):
        # partitions with measured business impact (e.g. reconciliation mismatches) scope the diagnosis
        ctx["affected_partitions"] = sorted(quality["affected_partitions"])
    finding, info = run_agent(
        "root_cause_investigator", state["incident_id"], ctx, round_=rnd, deadline=_deadline(state)
    )
    f: AgentFinding = finding
    before = set(state.get("evidence_refs", []))
    new_evidence = set(f.evidence_ids) - before
    route = _route_root_cause(state, f, rnd, bool(new_evidence))
    out = {
        "investigation_round": rnd,
        "agent_findings": [{"agent": "root_cause_investigator", "round": rnd, **f.model_dump(mode="json")}],
        "evidence_refs": sorted(new_evidence),
        "hypotheses": [h.model_dump() for h in f.hypotheses],
        "token_usage": _usage(info),
        "route": route,
        "tool_call_count": _tool_calls(state["incident_id"]),
        "affected_partitions": sorted(set(state.get("affected_partitions", [])) | set(f.affected_partitions)),
    }
    if f.conclusive:
        out["root_cause"] = {
            "category": f.root_cause,
            "summary": f.summary,
            "evidence_ids": f.evidence_ids,
            "uncertainty": f.uncertainty,
            "affected_partitions": sorted(f.affected_partitions or ctx["affected_partitions"]),
        }
    if route == "escalate":
        out["terminal_reason"] = _escalation_reason(state, f, rnd, bool(new_evidence))
    return out


def _route_root_cause(state: IncidentState, f: AgentFinding, rnd: int, new_evidence: bool) -> str:
    s = get_settings()
    if (
        state.get("elapsed_time", 0) >= s.budget_active_seconds
        or _tool_calls(state["incident_id"]) >= s.budget_tool_calls
    ):
        return "escalate"
    if f.conclusive and f.root_cause == "genuine_business_change" and f.recommended_next_step == "close_no_action":
        return "close_no_action"
    if f.conclusive and f.recommended_next_step == "propose_repair" and f.root_cause not in (None, "unknown"):
        return "repair_planner"
    if f.recommended_next_step == "investigate_more" and rnd < s.budget_investigation_rounds:
        if rnd > 1 and not new_evidence:
            return "escalate"
        return "root_cause"
    return "escalate"


def _escalation_reason(state, f: AgentFinding, rnd: int, new_evidence: bool) -> str:
    s = get_settings()
    if state.get("elapsed_time", 0) >= s.budget_active_seconds:
        return "active-time budget exhausted"
    if _tool_calls(state["incident_id"]) >= s.budget_tool_calls:
        return f"tool-call budget of {s.budget_tool_calls} exhausted"
    if rnd > 1 and not new_evidence and f.recommended_next_step == "investigate_more":
        return "repeated investigation produced no new evidence"
    if f.conclusive and f.recommended_next_step == "escalate":
        return f"diagnosed {f.root_cause}, but no safe automated repair exists: {f.summary}"
    if rnd >= s.budget_investigation_rounds:
        return f"investigation rounds exhausted ({rnd}) without a supported root cause"
    return f"escalated by investigation: {f.summary}"


@node("single_investigation")
def single_investigation(state: IncidentState) -> dict:
    """Evaluation baseline: one generalist agent replaces the three investigators."""
    s = get_settings()
    finding, info = run_agent("single_agent", state["incident_id"], _base_ctx(state), deadline=_deadline(state))
    f: AgentFinding = finding
    rnd = s.budget_investigation_rounds  # the single agent runs its own rounds inside one loop
    route = _route_root_cause(state, f, rnd, True)
    out = {
        "investigation_round": 1,
        "agent_findings": [{"agent": "single_agent", "round": 1, **f.model_dump(mode="json")}],
        "evidence_refs": f.evidence_ids,
        "hypotheses": [h.model_dump() for h in f.hypotheses],
        "token_usage": _usage(info),
        "route": route,
        "affected_partitions": sorted(set(state.get("affected_partitions", [])) | set(f.affected_partitions)),
    }
    if f.conclusive:
        out["root_cause"] = {
            "category": f.root_cause,
            "summary": f.summary,
            "evidence_ids": f.evidence_ids,
            "uncertainty": f.uncertainty,
            "affected_partitions": sorted(f.affected_partitions or state.get("affected_partitions", [])),
        }
    if route == "escalate":
        out["terminal_reason"] = _escalation_reason(state, f, rnd, True)
    return out


@node("repair_planner")
def repair_planner(state: IncidentState) -> dict:
    s = get_settings()
    attempt = state.get("repair_attempt", 0) + 1
    if attempt > s.budget_repair_attempts:
        return {"route": "escalate", "terminal_reason": f"repair attempts exhausted ({s.budget_repair_attempts})"}
    ctx = {
        **_base_ctx(state),
        "root_cause": (state.get("root_cause") or {}).get("category"),
        "root_cause_summary": (state.get("root_cause") or {}).get("summary"),
        "repair_attempt": attempt,
    }
    if (state.get("root_cause") or {}).get("affected_partitions"):
        ctx["affected_partitions"] = state["root_cause"]["affected_partitions"]
    draft, info = run_agent(
        "repair_planner",
        state["incident_id"],
        ctx,
        round_=attempt,
        feedback=state.get("feedback", []),
        deadline=_deadline(state),
    )
    d: RepairProposalDraft = draft
    base = {"repair_attempt": attempt, "token_usage": _usage(info), "draft": d.model_dump(mode="json")}
    if d.kind == "escalate":
        return {**base, "route": "escalate", "terminal_reason": d.escalation_reason or d.summary}
    if d.kind == "no_action":
        return {**base, "route": "escalate", "terminal_reason": "planner proposed no action for a diagnosed defect"}
    res = tools.invoke(
        tools.ToolContext("repair_planner", state["incident_id"]),
        "propose_patch",
        {**d.model_dump(mode="json"), "revision": attempt},
        operation_id=f"{state['incident_id']}:propose:{attempt}",
    )
    if res.status != "ok":
        return {**base, "route": "escalate", "terminal_reason": f"could not record proposal: {res.error}"}
    return {
        **base,
        "route": "policy_check",
        "proposal_id": res.output["proposal_id"],
        "proposal_hash": res.output["patch_hash"],
    }


@node("policy_check")
def policy_check(state: IncidentState) -> dict:
    d = RepairProposalDraft.model_validate(state["draft"])
    with new_session() as db:
        prop = db.get(RepairProposal, state["proposal_id"])
        result = policy.evaluate(
            d, affected_partitions=state.get("affected_partitions", []), base_commit=prop.base_commit
        )
        prop.policy_result = {
            **prop.policy_result,
            "ok": result.ok,
            "violations": result.violations,
            "checks": result.checks,
        }
        prop.status = "policy_passed" if result.ok else "policy_rejected"
        audit(
            db,
            "system:policy",
            "proposal.policy_" + ("passed" if result.ok else "rejected"),
            "proposal",
            prop.id,
            incident_id=prop.incident_id,
            detail={"violations": result.violations, "claimed_confidence": float(prop.claimed_confidence or 0)},
        )
        publish(
            db,
            prop.incident_id,
            "proposal.policy",
            {"proposal_id": prop.id, "ok": result.ok, "violations": result.violations},
        )
        db.commit()
    if result.ok:
        return {"route": "shadow_validate"}
    feedback = [
        *state.get("feedback", []),
        {"stage": "policy", "proposal_id": state["proposal_id"], "violations": result.violations},
    ]
    route = "repair_planner" if state.get("repair_attempt", 0) < get_settings().budget_repair_attempts else "escalate"
    return {
        "route": route,
        "feedback": feedback,
        "terminal_reason": None
        if route == "repair_planner"
        else "every repair proposal was rejected by policy: " + "; ".join(result.violations)[:500],
    }


@node("shadow_validate")
def shadow_validate(state: IncidentState) -> dict:
    iid, pid = state["incident_id"], state["proposal_id"]
    ctx = tools.ToolContext("controller", iid)
    env = tools.invoke(ctx, "create_shadow_environment", {"proposal_id": pid}, operation_id=f"{iid}:shadow:{pid}")
    if env.status != "ok":
        return {
            "validation_results": [
                {"check_id": "shadow_environment", "status": "error", "observed": {"error": env.error}}
            ]
        }
    run = tools.invoke(ctx, "run_shadow_pipeline", {"proposal_id": pid}, operation_id=f"{iid}:shadowrun:{pid}")
    out = run.output or {}
    val = tools.invoke(
        ctx,
        "run_protected_validation",
        {
            "proposal_id": pid,
            "build_ok": bool(out.get("build_ok")) and run.status == "ok",
            "build_detail": out.get("build_log_tail", run.error or ""),
            "tests_ok": out.get("tests_ok"),
            "tests_detail": ", ".join(out.get("test_failures", [])),
        },
        operation_id=f"{iid}:validate:{pid}",
    )
    if val.status != "ok":
        return {
            "validation_results": [
                {"check_id": "protected_validation", "status": "error", "observed": {"error": val.error}}
            ]
        }
    return {
        "validation_results": [
            {k: r[k] for k in ("check_id", "status", "expected", "observed")} for r in val.output["results"]
        ]
    }


@node("verification")
def verification(state: IncidentState) -> dict:
    iid, pid = state["incident_id"], state["proposal_id"]
    results = list(state.get("validation_results", []))
    attempt = state.get("repair_attempt", 1)
    with new_session() as db:
        prop = db.get(RepairProposal, pid)
        pinfo = {
            "proposal_id": pid,
            "kind": prop.kind,
            "summary": prop.summary,
            "partition_scope": prop.partition_scope,
            "claimed_confidence": float(prop.claimed_confidence or 0),
        }
    ctx = {**_base_ctx(state), "proposal": pinfo, "validation_results": results, "required_checks": STANDARD_CHECKS}
    verdict, info = run_agent("verification_analyst", iid, ctx, round_=attempt * 10)
    v: VerificationVerdict = verdict
    usage = _usage(info)
    if v.decision == "request_checks" and v.requested_checks:
        extra = tools.invoke(
            tools.ToolContext("verification_analyst", iid),
            "run_protected_validation",
            {"proposal_id": pid, "checks": v.requested_checks},
            operation_id=f"{iid}:validate_extra:{pid}",
        )
        if extra.status == "ok":
            results += [
                {k: r[k] for k in ("check_id", "status", "expected", "observed")} for r in extra.output["results"]
            ]
        verdict, info2 = run_agent(
            "verification_analyst",
            iid,
            {**ctx, "validation_results": results, "additional_checks_requested": True},
            round_=attempt * 10 + 1,
        )
        v = verdict
        usage = _merge_usage(usage, _usage(info2))
    failed = [r for r in results if r["status"] != "pass"]
    # Deterministic rule: the analyst may reject, but can never overrule a failed protected check.
    accepted = v.decision == "accept" and not failed and results
    with new_session() as db:
        prop = db.get(RepairProposal, pid)
        if not accepted:
            prop.status = "validation_failed" if failed else "rejected_by_analyst"
        audit(
            db,
            "agent:verification_analyst",
            "proposal.verdict",
            "proposal",
            pid,
            incident_id=iid,
            detail={
                "decision": "accept" if accepted else "reject",
                "analyst_decision": v.decision,
                "failed_checks": [r["check_id"] for r in failed],
                "reasons": v.reasons,
            },
        )
        db.commit()
    if accepted:
        return {"route": "request_approval", "validation_results": results, "token_usage": usage}
    feedback = [
        *state.get("feedback", []),
        {
            "stage": "validation",
            "proposal_id": pid,
            "failed_checks": [{"check_id": r["check_id"], "observed": r.get("observed")} for r in failed][:10],
            "analyst_reasons": v.reasons,
        },
    ]
    route = "repair_planner" if attempt < get_settings().budget_repair_attempts else "escalate"
    return {
        "route": route,
        "feedback": feedback,
        "validation_results": results,
        "token_usage": usage,
        "terminal_reason": None
        if route == "repair_planner"
        else "no proposal passed protected validation: " + ", ".join(r["check_id"] for r in failed)[:400],
    }


@node("request_approval")
def request_approval(state: IncidentState) -> dict:
    iid, pid = state["incident_id"], state["proposal_id"]
    res = tools.invoke(
        tools.ToolContext("controller", iid),
        "request_approval",
        {"proposal_id": pid},
        operation_id=f"{iid}:approval:{pid}",
    )
    if res.status != "ok":
        return {"route": "escalate", "terminal_reason": f"approval request failed: {res.error}"}
    approval_id = res.output["approval_id"]
    if _status(iid) not in ("awaiting_approval",):
        _set_status(iid, "awaiting_approval")
    interrupt({"approval_id": approval_id, "proposal_id": pid})  # durable pause; resumes on a decision
    with new_session() as db:
        appr = db.get(Approval, approval_id)
        from datawarden.services.approvals import expire_if_due

        expire_if_due(db, appr)
        db.commit()
        status = appr.status
    if status == "approved":
        return {"route": "execute", "approval_id": approval_id, "approval_status": status}
    if status == "invalidated" and state.get("repair_attempt", 0) < get_settings().budget_repair_attempts:
        # a changed patch/scope or stale snapshot: draft an explicit revision with a new approval request
        with new_session() as db:
            why = db.get(Approval, approval_id).invalidation_reason
        _set_status(iid, "investigating")
        return {
            "route": "repair_planner",
            "approval_id": approval_id,
            "approval_status": status,
            "feedback": [*state.get("feedback", []), {"stage": "approval", "problems": [why]}],
        }
    reason = {
        "rejected": "proposal rejected by approver; rejection terminates this proposal",
        "expired": "approval expired",
        "invalidated": "approval invalidated",
    }.get(status, f"approval {status}")
    return {"route": "escalate", "approval_id": approval_id, "approval_status": status, "terminal_reason": reason}


@node("execute")
def execute(state: IncidentState) -> dict:
    iid = state["incident_id"]
    _set_status(iid, "recovering")
    res = tools.invoke(
        tools.ToolContext("executor", iid),
        "execute_approved_repair",
        {"approval_id": state["approval_id"]},
        operation_id=f"{iid}:execute:{state['approval_id']}",
    )
    out = res.output or {"status": "error", "error": res.error}
    status = out.get("status")
    route = {"succeeded": "resolve", "manual_intervention": "manual_intervention"}.get(status)
    reason = None
    if status == "invalidated":
        if state.get("repair_attempt", 0) < get_settings().budget_repair_attempts:
            route = "repair_planner"
        else:
            route, reason = "escalate", "approval invalidated before execution: " + "; ".join(out.get("problems", []))
    elif route is None:
        route = "escalate"
        reason = {
            "rolled_back": "repair failed canonical verification and was rolled back (verified)",
            "conflict": "another repair holds overlapping partitions",
        }.get(status, f"execution {status}: {out.get('error')}")
    fb = state.get("feedback", [])
    if status == "invalidated":
        fb = [*fb, {"stage": "approval", "problems": out.get("problems", [])}]
        _set_status(iid, "investigating")
    return {
        "route": route,
        "recovery": out,
        "rollback_ref": out.get("operation_id"),
        "terminal_reason": reason,
        "feedback": fb,
    }


def _close(state: IncidentState, status: str, reason: str) -> dict:
    iid = state["incident_id"]
    with new_session() as db:
        inc = db.get(Incident, iid)
        evidence = db.scalar(select(func.count()).select_from(Evidence).where(Evidence.incident_id == iid))
        inc.status = status
        inc.terminal_reason = reason
        inc.closed_at = datetime.now(UTC)
        inc.version += 1
        inc.summary = {
            **(inc.summary or {}),
            "root_cause": state.get("root_cause"),
            "proposal_id": state.get("proposal_id"),
            "approval_status": state.get("approval_status"),
            "recovery": state.get("recovery"),
            "investigation_rounds": state.get("investigation_round"),
            "repair_attempts": state.get("repair_attempt"),
            "evidence_count": evidence,
            "active_seconds": round(state.get("elapsed_time", 0.0), 2),
            "token_usage": state.get("token_usage", {}),
            "graph_version": GRAPH_VERSION,
        }
        usage = dict(inc.usage or {})
        usage.update({"active_seconds": round(state.get("elapsed_time", 0.0), 2), **state.get("token_usage", {})})
        inc.usage = usage
        audit(
            db, "system:controller", f"incident.{status}", "incident", iid, incident_id=iid, detail={"reason": reason}
        )
        publish(db, iid, "incident.status", {"status": status, "reason": reason})
        db.commit()
    return {"terminal_reason": reason, "route": "end"}


@node("resolve")
def resolve(state: IncidentState) -> dict:
    return _close(state, "resolved", "repair applied and canonical outputs verified")


@node("close_no_action")
def close_no_action(state: IncidentState) -> dict:
    rc = state.get("root_cause") or {}
    return _close(state, "closed_no_action", f"investigated; no repair required: {rc.get('summary', '')}"[:500])


@node("escalate")
def escalate(state: IncidentState) -> dict:
    return _close(state, "escalated", state.get("terminal_reason") or "escalated")


@node("manual_intervention")
def manual_intervention(state: IncidentState) -> dict:
    return _close(state, "manual_intervention", "rollback could not be verified; manual intervention required")


def _router(*allowed: str):
    def route(state: IncidentState) -> str:
        r = state.get("route")
        if r in ("cancelled", "skip", "end") or r is None:
            return END
        return r if r in allowed else "escalate"

    return route


def build_graph(checkpointer=None, variant: str = "multi"):
    """``variant='multi'`` is the product graph; ``'single'`` is the single-agent evaluation baseline."""
    g = StateGraph(IncidentState)
    investigation = (
        [
            ("quality_investigator", quality_investigator),
            ("lineage_investigator", lineage_investigator),
            ("root_cause", root_cause),
        ]
        if variant == "multi"
        else [("single_investigation", single_investigation)]
    )
    for name, fn in [
        ("intake", intake),
        ("load_context", load_context),
        *investigation,
        ("repair_planner", repair_planner),
        ("policy_check", policy_check),
        ("shadow_validate", shadow_validate),
        ("verification", verification),
        ("request_approval", request_approval),
        ("execute", execute),
        ("resolve", resolve),
        ("close_no_action", close_no_action),
        ("escalate", escalate),
        ("manual_intervention", manual_intervention),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_conditional_edges(
        "intake", lambda s: END if s.get("route") != "ok" else "load_context", ["load_context", END]
    )
    if variant == "multi":
        g.add_edge("load_context", "quality_investigator")
        g.add_edge("load_context", "lineage_investigator")
        g.add_edge(["quality_investigator", "lineage_investigator"], "root_cause")
        g.add_conditional_edges(
            "root_cause",
            _router("root_cause", "repair_planner", "close_no_action", "escalate"),
            ["root_cause", "repair_planner", "close_no_action", "escalate", END],
        )
    else:
        g.add_edge("load_context", "single_investigation")
        g.add_conditional_edges(
            "single_investigation",
            _router("repair_planner", "close_no_action", "escalate"),
            ["repair_planner", "close_no_action", "escalate", END],
        )
    g.add_conditional_edges("repair_planner", _router("policy_check", "escalate"), ["policy_check", "escalate", END])
    g.add_conditional_edges(
        "policy_check",
        _router("shadow_validate", "repair_planner", "escalate"),
        ["shadow_validate", "repair_planner", "escalate", END],
    )
    g.add_edge("shadow_validate", "verification")
    g.add_conditional_edges(
        "verification",
        _router("request_approval", "repair_planner", "escalate"),
        ["request_approval", "repair_planner", "escalate", END],
    )
    g.add_conditional_edges(
        "request_approval",
        _router("execute", "repair_planner", "escalate"),
        ["execute", "repair_planner", "escalate", END],
    )
    g.add_conditional_edges(
        "execute",
        _router("resolve", "repair_planner", "escalate", "manual_intervention"),
        ["resolve", "repair_planner", "escalate", "manual_intervention", END],
    )
    for t in ("resolve", "close_no_action", "escalate", "manual_intervention"):
        g.add_edge(t, END)
    return g.compile(checkpointer=checkpointer)
