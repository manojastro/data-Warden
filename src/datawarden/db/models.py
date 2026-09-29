"""Application database schema (SQLAlchemy 2.0). Migrations live in ``migrations/``."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONB, list: JSONB}


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --- identity -------------------------------------------------------------------------------


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = _now()


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the bearer token
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- catalog --------------------------------------------------------------------------------


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # raw | staging | dimension | fact | mart
    schema_name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(64), default="")
    business_critical: Mapped[bool] = mapped_column(Boolean, default=False)
    columns: Mapped[list] = mapped_column(JSONB, default=list)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class LineageEdge(Base):
    __tablename__ = "lineage_edges"
    __table_args__ = (UniqueConstraint("upstream_asset_id", "downstream_asset_id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    upstream_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    downstream_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32), default="dbt_manifest")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    source_watermark: Mapped[int | None] = mapped_column(Integer)
    code_commit: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    reported_at: Mapped[datetime] = _now()


class QualityCheck(Base):
    __tablename__ = "quality_checks"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    check_type: Mapped[str] = mapped_column(String(32))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    severity: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text, default="")
    threshold: Mapped[dict] = mapped_column(JSONB, default=dict)
    protected: Mapped[bool] = mapped_column(Boolean, default=True)


class QualityResult(Base):
    __tablename__ = "quality_results"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    check_id: Mapped[str] = mapped_column(ForeignKey("quality_checks.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.id"))
    status: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(16))
    partition_date: Mapped[date | None] = mapped_column(Date)
    observed: Mapped[dict] = mapped_column(JSONB, default=dict)
    message: Mapped[str] = mapped_column(Text)
    evaluated_at: Mapped[datetime] = _now()


# --- incidents ------------------------------------------------------------------------------


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("inc"))
    number: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    asset_ids: Mapped[list] = mapped_column(JSONB, default=list)
    check_ids: Mapped[list] = mapped_column(JSONB, default=list)
    affected_partitions: Mapped[list] = mapped_column(JSONB, default=list)
    run_id: Mapped[str | None] = mapped_column(String(64))
    correlation_key: Mapped[str] = mapped_column(String(128), index=True)
    graph_thread_id: Mapped[str | None] = mapped_column(String(64))
    graph_version: Mapped[str | None] = mapped_column(String(32))
    model_mode: Mapped[str | None] = mapped_column(String(16))
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("iev"))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(32))
    check_id: Mapped[str | None] = mapped_column(String(128))
    asset_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), index=True)
    received_at: Mapped[datetime] = _now()


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("arun"))
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    agent: Mapped[str] = mapped_column(String(32))
    round: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16))
    model_mode: Mapped[str] = mapped_column(String(16))
    decision_summary: Mapped[str] = mapped_column(Text, default="")
    finding: Mapped[dict] = mapped_column(JSONB, default=dict)
    uncertainty: Mapped[str | None] = mapped_column(String(16))
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    agent: Mapped[str | None] = mapped_column(String(32))
    asset_id: Mapped[str | None] = mapped_column(String(64))
    query_or_tool_ref: Mapped[str] = mapped_column(Text)
    artifact_hash: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    redaction_status: Mapped[str] = mapped_column(String(16))
    untrusted_text: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    collected_at: Mapped[datetime] = _now()


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    category: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    supporting_evidence_ids: Mapped[list] = mapped_column(JSONB, default=list)
    contradicting_evidence_ids: Mapped[list] = mapped_column(JSONB, default=list)
    next_query: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("tc"))
    operation_id: Mapped[str] = mapped_column(String(128), unique=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), index=True)
    agent: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(48))
    input: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16))  # ok | error | denied | timeout
    output: Mapped[dict | None] = mapped_column(JSONB)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    output_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = _now()


class RepairProposal(Base):
    __tablename__ = "repair_proposals"
    __table_args__ = (UniqueConstraint("incident_id", "revision"),)
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))  # dbt_patch | mapping_patch | replay
    summary: Mapped[str] = mapped_column(Text)
    patch: Mapped[str] = mapped_column(Text, default="")
    patch_hash: Mapped[str] = mapped_column(String(64))
    base_commit: Mapped[str] = mapped_column(String(64))
    source_watermark: Mapped[int] = mapped_column(Integer)
    asset_scope: Mapped[list] = mapped_column(JSONB, default=list)
    partition_scope: Mapped[list] = mapped_column(JSONB, default=list)
    preconditions: Mapped[list] = mapped_column(JSONB, default=list)
    risks: Mapped[list] = mapped_column(JSONB, default=list)
    rollback_plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    claimed_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(String(24))
    policy_result: Mapped[dict] = mapped_column(JSONB, default=dict)
    shadow_schema: Mapped[str | None] = mapped_column(String(64))
    comparison: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()


class Validation(Base):
    __tablename__ = "validations"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("repair_proposals.id"), index=True)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict] = mapped_column(JSONB)
    check_id: Mapped[str] = mapped_column(String(128))
    expected: Mapped[dict] = mapped_column(JSONB, default=dict)
    observed: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16))
    artifact_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("apr"))
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("repair_proposals.id"), unique=True)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    base_commit: Mapped[str] = mapped_column(String(64))
    source_watermark: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # pending | approved | rejected | expired | invalidated
    requested_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    invalidation_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)


class RecoveryOperation(Base):
    __tablename__ = "recovery_operations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rop"))
    operation_key: Mapped[str] = mapped_column(String(160), unique=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("repair_proposals.id"))
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"))
    status: Mapped[str] = mapped_column(String(24))
    journal: Mapped[list] = mapped_column(JSONB, default=list)
    lock_keys: Mapped[list] = mapped_column(JSONB, default=list)
    snapshot_ref: Mapped[str | None] = mapped_column(Text)
    snapshot_checksum: Mapped[str | None] = mapped_column(String(64))
    pre_commit: Mapped[str | None] = mapped_column(String(64))
    post_commit: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartitionLock(Base):
    """Asset/partition locks preventing concurrent repair of overlapping scopes."""

    __tablename__ = "partition_locks"
    lock_key: Mapped[str] = mapped_column(String(160), primary_key=True)  # asset:date
    operation_id: Mapped[str] = mapped_column(ForeignKey("recovery_operations.id"))
    acquired_at: Mapped[datetime] = _now()


# --- audit, stream, jobs --------------------------------------------------------------------


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = _now()
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class StreamEvent(Base):
    """Persisted live-progress events; their ids are SSE event ids (resume via Last-Event-ID)."""

    __tablename__ = "stream_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("jobs_ready_idx", "status", "run_after"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job"))
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed|dead
    dedup_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = _now()
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seen_at: Mapped[datetime] = _now()
    info: Mapped[dict] = mapped_column(JSONB, default=dict)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("art"))
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class ModelUsage(Base):
    __tablename__ = "model_usage"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(String(40), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(40))
    agent: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))  # fixture | live
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = _now()


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("eval"))
    status: Mapped[str] = mapped_column(String(16))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    results: Mapped[dict] = mapped_column(JSONB, default=dict)
    report_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
