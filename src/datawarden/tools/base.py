"""Typed tool framework: allowlists, input validation, atomic budgets, timeouts, bounded output,
persisted tool calls, and immutable evidence records.

Permission enforcement lives here, in code, not in prompts. A caller that is not on a tool's
allowlist gets a ``denied`` result that is recorded in ``tool_calls`` and ``audit_events``.
Tool results are persisted *before* they are returned, so a resumed graph re-reads the stored
result for the same ``operation_id`` instead of executing the tool twice.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from datawarden.db.models import Evidence, ToolCall
from datawarden.db.session import new_session
from datawarden.logs import redact
from datawarden.services.audit import audit, publish

log = logging.getLogger(__name__)
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

AGENTS = (
    "quality_investigator",
    "lineage_investigator",
    "root_cause_investigator",
    "repair_planner",
    "verification_analyst",
    "single_agent",
)


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[ToolContext, BaseModel], dict]
    allowed_callers: frozenset[str]
    timeout_s: float = 20.0
    mutating: bool = False
    counts_toward_budget: bool = True
    max_output_bytes: int = 16_000
    evidence_asset: Callable[[BaseModel], str | None] = lambda _args: None


@dataclass
class ToolContext:
    caller: str
    incident_id: str | None = None
    tool_budget: int | None = None
    request_id: str | None = None
    extra: dict = field(default_factory=dict)


class ToolResult(BaseModel):
    tool: str
    status: str  # ok | error | denied | timeout | budget_exhausted
    output: dict | None = None
    evidence_id: str | None = None
    tool_call_id: str | None = None
    error: str | None = None
    truncated: bool = False
    replayed: bool = False


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    REGISTRY[spec.name] = spec
    return spec


def tool_schemas(caller: str) -> list[dict]:
    """JSON schemas of the tools a caller may use (what a model is shown)."""
    return [
        {"name": s.name, "description": s.description, "parameters": s.input_model.model_json_schema()}
        for s in REGISTRY.values()
        if caller in s.allowed_callers
    ]


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _bound(output: dict, max_bytes: int) -> tuple[dict, bool]:
    raw = json.dumps(output, default=str)
    if len(raw) <= max_bytes:
        return json.loads(raw), False
    # shrink the largest lists until the payload fits
    out = json.loads(raw)
    for _ in range(20):
        lists = [(k, v) for k, v in out.items() if isinstance(v, list) and len(v) > 1 and k != "_truncated"]
        if not lists:
            break
        k, v = max(lists, key=lambda kv: len(json.dumps(kv[1], default=str)))
        out[k] = v[: max(1, len(v) // 2)]
        truncated_keys = out.setdefault("_truncated", [])
        if k not in truncated_keys:
            truncated_keys.append(k)
        if len(json.dumps(out, default=str)) <= max_bytes:
            return out, True
    text_ = json.dumps(out, default=str)[:max_bytes]
    return {"_truncated_text": text_}, True


def _consume_budget(db: Session, incident_id: str, limit: int) -> bool:
    row = db.execute(
        text("""
        UPDATE incidents SET usage = jsonb_set(coalesce(usage, '{}'::jsonb), '{tool_calls}',
                 to_jsonb(coalesce((usage->>'tool_calls')::int, 0) + 1))
        WHERE id = :id AND coalesce((usage->>'tool_calls')::int, 0) < :limit
        RETURNING (usage->>'tool_calls')::int AS n"""),
        {"id": incident_id, "limit": limit},
    ).first()
    return row is not None


def invoke(ctx: ToolContext, name: str, args: dict | None = None, *, operation_id: str | None = None) -> ToolResult:
    args = args or {}
    spec = REGISTRY.get(name)
    op_id = operation_id or f"{ctx.incident_id or 'none'}:{ctx.caller}:{name}:{_hash(args)[:16]}:{time.time_ns()}"
    with new_session() as db:
        prior = db.scalar(select(ToolCall).where(ToolCall.operation_id == op_id))
        if prior is not None:  # replay after crash/resume: never execute twice
            ev = db.scalar(select(Evidence.id).where(Evidence.tool_call_id == prior.id))
            return ToolResult(
                tool=prior.tool,
                status=prior.status,
                output=prior.output,
                evidence_id=ev,
                tool_call_id=prior.id,
                error=prior.error,
                replayed=True,
            )

        def record(
            status: str,
            output: dict | None = None,
            error: str | None = None,
            duration_ms: int = 0,
            truncated: bool = False,
            args_: dict = args,
        ) -> ToolResult:
            out_json = json.dumps(output, default=str) if output is not None else ""
            tc = ToolCall(
                operation_id=op_id,
                incident_id=ctx.incident_id,
                agent=ctx.caller,
                tool=name,
                input=json.loads(json.dumps(args_, default=str)),
                status=status,
                output=output,
                output_hash=_hash(output) if output is not None else None,
                output_bytes=len(out_json),
                error=redact(error)[:2000] if error else None,
                duration_ms=duration_ms,
            )
            db.add(tc)
            db.flush()
            evidence_id = None
            if status == "ok" and ctx.incident_id and spec is not None:
                artifact_hash = _hash({"tool": name, "args": args_, "output": output})
                evidence_id = f"E-{ctx.incident_id[-6:]}-{artifact_hash[:10]}"
                if db.get(Evidence, evidence_id) is None:
                    untrusted = bool(output and output.get("contains_untrusted_text"))
                    db.add(
                        Evidence(
                            id=evidence_id,
                            incident_id=ctx.incident_id,
                            tool_call_id=tc.id,
                            agent=ctx.caller,
                            asset_id=spec.evidence_asset(spec.input_model.model_validate(args_)),
                            query_or_tool_ref=f"{name}({json.dumps(args_, sort_keys=True, default=str)[:400]})",
                            artifact_hash=artifact_hash,
                            summary=str((output or {}).get("summary", ""))[:1000],
                            redaction_status=(output or {}).get("redaction_status", "not_applicable"),
                            untrusted_text=untrusted,
                            data=output or {},
                        )
                    )
            if status in ("denied", "budget_exhausted"):
                audit(
                    db,
                    f"agent:{ctx.caller}",
                    f"tool.{status}",
                    "tool",
                    name,
                    incident_id=ctx.incident_id,
                    detail={"args": json.loads(json.dumps(args_, default=str)), "reason": error},
                    request_id=ctx.request_id,
                )
            if ctx.incident_id:
                publish(
                    db,
                    ctx.incident_id,
                    "tool.call",
                    {
                        "agent": ctx.caller,
                        "tool": name,
                        "status": status,
                        "evidence_id": evidence_id,
                        "summary": str((output or {}).get("summary", error or ""))[:300],
                    },
                )
            db.commit()
            return ToolResult(
                tool=name,
                status=status,
                output=output,
                evidence_id=evidence_id,
                tool_call_id=tc.id,
                error=error,
                truncated=truncated,
            )

        if spec is None:
            return record("denied", error=f"unknown tool {name}")
        if ctx.caller not in spec.allowed_callers:
            return record("denied", error=f"{ctx.caller} is not permitted to call {name}")
        try:
            parsed = spec.input_model.model_validate(args)
        except ValidationError as exc:
            return record("error", error=f"invalid arguments: {exc.errors()[:3]}")
        if spec.counts_toward_budget and ctx.incident_id and ctx.tool_budget is not None:
            if not _consume_budget(db, ctx.incident_id, ctx.tool_budget):
                db.commit()
                return record("budget_exhausted", error=f"tool-call budget of {ctx.tool_budget} exhausted")
            db.commit()
        started = time.monotonic()
        future = _pool.submit(spec.fn, ctx, parsed)
        try:
            output = future.result(timeout=spec.timeout_s)
        except concurrent.futures.TimeoutError:
            return record(
                "timeout",
                error=f"{name} exceeded {spec.timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except ToolError as exc:
            return record("error", error=str(exc), duration_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - tool failures are data, not crashes
            log.exception("tool %s failed", name)
            return record(
                "error", error=f"{type(exc).__name__}: {exc}", duration_ms=int((time.monotonic() - started) * 1000)
            )
        bounded, truncated = _bound(output, spec.max_output_bytes)
        return record("ok", bounded, duration_ms=int((time.monotonic() - started) * 1000), truncated=truncated)
