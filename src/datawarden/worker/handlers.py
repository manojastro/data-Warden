"""Job handlers. Each handler must be safe to re-run after a crash (idempotent)."""

from __future__ import annotations

import logging

from datawarden.worker.main import handler

log = logging.getLogger(__name__)


def _run_and_emit(trigger: str, **kwargs) -> dict:
    from datawarden.events.emitter import emit_run_events
    from datawarden.pipeline.runner import run_pipeline

    report = run_pipeline(trigger=trigger, **kwargs)
    emitted = emit_run_events(report)
    return {"run": report.summary(), "emitted": emitted}


@handler("pipeline_run")
def pipeline_run(payload: dict) -> dict:
    return _run_and_emit(payload.get("trigger", "manual"))


@handler("demo_inject")
def demo_inject(payload: dict) -> dict:
    from datawarden.faults import injector

    state = injector.load_state()
    if not any(f["scenario"] == payload["scenario"] for f in state["active"]):
        injector.inject(payload["scenario"])  # idempotent on retry: skip if already injected
    if payload.get("run_pipeline", True):
        return _run_and_emit("scheduled")
    return {"injected": payload["scenario"]}


@handler("demo_reset")
def demo_reset(payload: dict) -> dict:
    from datawarden.faults import injector

    result = injector.reset(rebuild=False)
    result["pipeline"] = _run_and_emit("demo_reset", full_refresh=True)
    return result


@handler("investigate_incident")
def investigate_incident(payload: dict) -> dict:
    from datawarden.agents.runner import run_incident_graph

    return run_incident_graph(payload["incident_id"])


@handler("resume_incident")
def resume_incident(payload: dict) -> dict:
    from datawarden.agents.runner import resume_incident_graph

    return resume_incident_graph(payload["incident_id"], payload["approval_id"], payload["decision"])


@handler("evaluation")
def evaluation(payload: dict) -> dict:
    from datawarden.evals.runner import run_evaluation

    return run_evaluation(scenarios=payload.get("scenarios"), seeds=payload.get("seeds"), modes=payload.get("modes"))


@handler("send_webhook")
def send_webhook(payload: dict) -> dict:
    from datawarden.services.notify import deliver_webhook

    return deliver_webhook(payload)
