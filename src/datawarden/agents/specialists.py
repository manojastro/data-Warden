"""Five specialist agents. Each is a bounded model -> tool -> observation loop with its own tool
allowlist (enforced in ``tools.base``), step limit, and structured output contract."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from datawarden.agents.models import AgentTurn, ModelError, get_provider, validate_final
from datawarden.config import get_settings
from datawarden.contracts.agents import AgentFinding, RepairProposalDraft, VerificationVerdict
from datawarden.db.models import AgentRun, Hypothesis, ModelUsage
from datawarden.db.session import new_session
from datawarden.services.audit import publish
from datawarden.tools import base as tools

log = logging.getLogger(__name__)
_slots = threading.BoundedSemaphore(get_settings().budget_concurrent_specialists)


@dataclass(frozen=True)
class AgentDef:
    name: str
    title: str
    role: str
    output: type[BaseModel]
    max_steps: int


AGENTS = {
    "quality_investigator": AgentDef(
        "quality_investigator",
        "the Quality Investigator",
        "Classify the violated checks, examine affected rows and distributions, and return observed symptoms, "
        "evidence ids, candidate explanations, and uncertainty. Do not diagnose beyond the evidence.",
        AgentFinding,
        9,
    ),
    "lineage_investigator": AgentDef(
        "lineage_investigator",
        "the Lineage and Impact Investigator",
        "Traverse dbt lineage (bounded depth) from the failing assets and identify affected transformations, "
        "partitions, and business reports. Return an impact map.",
        AgentFinding,
        4,
    ),
    "root_cause_investigator": AgentDef(
        "root_cause_investigator",
        "the Root Cause Investigator",
        "Examine run logs, schema history, code changes and targeted read-only SQL. Keep competing hypotheses, "
        "seek disconfirming evidence, and distinguish a real business change from a data defect. If evidence is "
        "insufficient, say so and name the next targeted query.",
        AgentFinding,
        8,
    ),
    "repair_planner": AgentDef(
        "repair_planner",
        "the Repair Planner",
        "Propose the smallest safe repair: a constrained dbt SQL patch, an ingestion mapping justified by a "
        "registered contract, a bounded replay, no action, or escalation. Declare assets, partitions, "
        "preconditions, risks, and rollback. You cannot write canonical data or approve anything. Never fabricate "
        "missing source records.",
        RepairProposalDraft,
        4,
    ),
    "verification_analyst": AgentDef(
        "verification_analyst",
        "the Verification Analyst",
        "Interpret the immutable protected validation results. Identify missing coverage and request permitted "
        "checks. You may reject a proposal but can never overrule a failed deterministic check, and you cannot edit "
        "rules, expected outcomes, or the repair.",
        VerificationVerdict,
        2,
    ),
    "single_agent": AgentDef(
        "single_agent",
        "a single generalist investigator (evaluation baseline)",
        "Investigate the incident end to end with the investigator tools.",
        AgentFinding,
        25,
    ),
}


def run_agent(
    agent: str,
    incident_id: str,
    context: dict,
    *,
    round_: int = 1,
    feedback: list[dict] | None = None,
    deadline: float | None = None,
) -> tuple[BaseModel, dict]:
    """Run one bounded agent loop. Returns (validated output, run info)."""
    spec = AGENTS[agent]
    s = get_settings()
    provider = get_provider()
    run_id = f"arun_{incident_id[-10:]}_{agent[:12]}_{round_}"
    with new_session() as db:
        existing = db.get(AgentRun, run_id)
        if existing and existing.status == "completed":  # replay after crash: reuse the stored finding
            return spec.output.model_validate(existing.finding), {"agent_run_id": run_id, "replayed": True}
        if existing is None:
            db.add(
                AgentRun(
                    id=run_id,
                    incident_id=incident_id,
                    agent=agent,
                    round=round_,
                    status="running",
                    model_mode=provider.mode,
                )
            )
        publish(db, incident_id, "agent.started", {"agent": agent, "round": round_, "mode": provider.mode})
        db.commit()

    observations: list[dict] = []
    ctx = tools.ToolContext(caller=agent, incident_id=incident_id, tool_budget=s.budget_tool_calls)
    usage_in = usage_out = 0
    final: dict | None = None
    stop_reason = ""
    seen_evidence: set[str] = set()
    stale = 0
    with _slots:
        for step in range(spec.max_steps + 1):
            if deadline and time.monotonic() > deadline:
                stop_reason = "active-time budget exhausted"
                break
            turn = AgentTurn(
                agent=agent,
                title=spec.title,
                role_prompt=spec.role,
                context=context,
                observations=observations,
                tools=tools.tool_schemas(agent),
                final_schema=spec.output.model_json_schema(),
                step=step,
                max_steps=spec.max_steps,
                feedback=feedback or [],
            )
            try:
                decision = provider.decide(turn)
            except ModelError as exc:
                stop_reason = f"model error: {exc}"
                break
            usage_in += decision.usage.input_tokens
            usage_out += decision.usage.output_tokens
            _record_usage(incident_id, run_id, agent, provider, decision.usage)
            if decision.kind == "final":
                final = decision.final
                break
            if step >= spec.max_steps:
                stop_reason = "step limit reached"
                break
            result = tools.invoke(
                ctx, decision.tool or "", decision.args or {}, operation_id=f"{incident_id}:{agent}:{round_}:{step}"
            )
            observations.append(
                {
                    "step": step,
                    "tool": decision.tool,
                    "args": decision.args or {},
                    "status": result.status,
                    "evidence_id": result.evidence_id,
                    "error": result.error,
                    "output": result.output,
                }
            )
            if result.status == "budget_exhausted":
                stop_reason = "tool-call budget exhausted"
                break
            if result.evidence_id and result.evidence_id in seen_evidence:
                stale += 1
                if stale >= 2:
                    stop_reason = "repeated identical queries without new evidence"
                    break
            elif result.evidence_id:
                seen_evidence.add(result.evidence_id)
    output, error = _finalize(spec, final, stop_reason, observations)
    info = {
        "agent_run_id": run_id,
        "tool_calls": len(observations),
        "stop_reason": stop_reason or "final",
        "input_tokens": usage_in,
        "output_tokens": usage_out,
        "error": error,
    }
    with new_session() as db:
        run = db.get(AgentRun, run_id)
        run.status = "completed" if not error else "failed"
        run.finding = output.model_dump(mode="json")
        run.decision_summary = _summary(output)[:1000]
        run.uncertainty = getattr(output, "uncertainty", None)
        run.tool_calls = len(observations)
        run.input_tokens, run.output_tokens = usage_in, usage_out
        run.error = error
        run.finished_at = datetime.now(UTC)
        if isinstance(output, AgentFinding):
            for i, h in enumerate(output.hypotheses):
                hid = f"H-{incident_id[-6:]}-{agent[:2]}{round_}-{i}"
                if db.get(Hypothesis, hid) is None:
                    db.add(
                        Hypothesis(
                            id=hid,
                            incident_id=incident_id,
                            agent_run_id=run_id,
                            category=h.category,
                            description=h.description,
                            status=h.status,
                            supporting_evidence_ids=h.supporting_evidence_ids,
                            contradicting_evidence_ids=h.contradicting_evidence_ids,
                            next_query=h.next_query,
                        )
                    )
        publish(
            db,
            incident_id,
            "agent.completed",
            {
                "agent": agent,
                "round": round_,
                "summary": run.decision_summary,
                "tool_calls": len(observations),
                "stop_reason": info["stop_reason"],
                "uncertainty": run.uncertainty,
            },
        )
        db.commit()
    return output, info


def _finalize(
    spec: AgentDef, final: dict | None, stop_reason: str, observations: list[dict]
) -> tuple[BaseModel, str | None]:
    evidence = [o["evidence_id"] for o in observations if o.get("evidence_id")]
    if final is not None:
        try:
            out = validate_final(spec.output, final)
            if isinstance(out, AgentFinding):
                out.agent_id = spec.name
                known = set(evidence)
                out.evidence_ids = sorted(set(out.evidence_ids) | known)
            return out, None
        except ModelError as exc:
            stop_reason = str(exc)
    if spec.output is AgentFinding:
        return AgentFinding(
            agent_id=spec.name,
            summary=f"stopped: {stop_reason}",
            evidence_ids=evidence,
            uncertainty="high",
            recommended_next_step="escalate",
        ), stop_reason
    if spec.output is RepairProposalDraft:
        return RepairProposalDraft(
            kind="escalate", summary=f"planner stopped: {stop_reason}", escalation_reason=stop_reason
        ), stop_reason
    return VerificationVerdict(decision="reject", reasons=[f"analyst stopped: {stop_reason}"]), stop_reason


def _summary(output: BaseModel) -> str:
    if isinstance(output, AgentFinding):
        return output.summary
    if isinstance(output, RepairProposalDraft):
        return f"{output.kind}: {output.summary}"
    if isinstance(output, VerificationVerdict):
        return f"{output.decision}: " + "; ".join(output.reasons or output.requested_checks)
    return str(output)[:500]


def _record_usage(incident_id: str, run_id: str, agent: str, provider, usage) -> None:
    from datawarden.agents.models import estimate_cost

    with new_session() as db:
        db.add(
            ModelUsage(
                incident_id=incident_id,
                agent_run_id=run_id,
                agent=agent,
                provider=provider.provider,
                model=provider.model,
                mode=provider.mode,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=usage.latency_ms,
                estimated_cost_usd=estimate_cost(usage),
            )
        )
        db.commit()
