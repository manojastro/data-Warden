"""Structured contracts exchanged between the controller and specialist agents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RootCause = Literal[
    "duplicate_source_events",
    "join_fanout",
    "schema_drift",
    "late_arriving_data",
    "transient_pipeline_failure",
    "genuine_business_change",
    "unknown",
]
Uncertainty = Literal["low", "medium", "high"]


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default="", max_length=48)
    category: RootCause = "unknown"
    description: str = Field(max_length=1000)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    status: Literal["proposed", "supported", "refuted", "inconclusive"] = "proposed"
    next_query: str | None = Field(default=None, max_length=500)


class AgentFinding(BaseModel):
    """Final output of an investigator. ``uncertainty`` is an uncalibrated self-assessment."""

    model_config = ConfigDict(extra="ignore")
    agent_id: str = ""
    summary: str = Field(max_length=2000)
    symptoms: list[str] = Field(default_factory=list, max_length=20)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=60)
    affected_assets: list[str] = Field(default_factory=list, max_length=20)
    affected_partitions: list[str] = Field(default_factory=list, max_length=60)
    business_reports: list[str] = Field(default_factory=list, max_length=10)
    root_cause: RootCause | None = None
    conclusive: bool = False
    recommended_next_step: Literal["investigate_more", "propose_repair", "close_no_action", "escalate", "none"] = "none"
    next_query: str | None = Field(default=None, max_length=500)
    uncertainty: Uncertainty = "high"


class RepairProposalDraft(BaseModel):
    """What the Repair Planner may produce. It cannot apply, approve, or edit validators."""

    model_config = ConfigDict(extra="ignore")
    kind: Literal["dbt_patch", "mapping_patch", "replay", "no_action", "escalate"]
    summary: str = Field(max_length=2000)
    files: dict[str, str] = Field(default_factory=dict, description="relative path -> full new file content")
    asset_scope: list[str] = Field(default_factory=list, max_length=20)
    partition_scope: list[str] = Field(default_factory=list, max_length=60)
    preconditions: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=10)
    rollback_plan: str = Field(default="", max_length=1000)
    claimed_confidence: float | None = Field(default=None, ge=0, le=1)
    escalation_reason: str | None = Field(default=None, max_length=1000)


class ValidationResult(BaseModel):
    proposal_hash: str
    input_snapshot: dict
    check_id: str
    expected: dict = Field(default_factory=dict)
    observed: dict = Field(default_factory=dict)
    status: Literal["pass", "fail", "error"]
    artifact_ref: str | None = None


class VerificationVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision: Literal["accept", "reject", "request_checks"]
    reasons: list[str] = Field(default_factory=list, max_length=10)
    requested_checks: list[str] = Field(default_factory=list, max_length=10)
    missing_coverage: list[str] = Field(default_factory=list, max_length=10)
    uncertainty: Uncertainty = "medium"
