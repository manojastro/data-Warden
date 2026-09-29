"""Entry points used by worker jobs: start/continue an investigation, resume after a decision."""

from __future__ import annotations

import logging
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row

from datawarden.agents.graph import build_graph, graph_version
from datawarden.config import get_settings
from datawarden.db.models import Incident
from datawarden.db.session import new_session
from datawarden.observability import span

log = logging.getLogger(__name__)
_setup_done = False


def _variant(incident_id: str) -> str:
    """New investigations use the configured variant; resumes use the variant they started with."""
    with new_session() as db:
        inc = db.get(Incident, incident_id)
        if inc is not None and inc.graph_version:
            return inc.graph_version.split("+", 1)[1] if "+" in inc.graph_version else "multi"
        variant = get_settings().graph_variant
        if inc is not None:
            inc.graph_version = graph_version(variant)
            db.commit()
        return variant


@contextmanager
def checkpointed_graph(incident_id: str | None = None):
    global _setup_done
    variant = _variant(incident_id) if incident_id else "multi"
    with psycopg.connect(get_settings().app_dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row) as conn:
        saver = PostgresSaver(conn)
        if not _setup_done:
            saver.setup()
            _setup_done = True
        yield build_graph(saver, variant)


def _config(incident_id: str) -> dict:
    return {"configurable": {"thread_id": incident_id}, "recursion_limit": 80}


def _summary(incident_id: str, state: dict | None = None) -> dict:
    with new_session() as db:
        inc = db.get(Incident, incident_id)
        return {
            "incident_id": incident_id,
            "status": inc.status if inc else None,
            "terminal_reason": inc.terminal_reason if inc else None,
            "route": (state or {}).get("route"),
        }


def run_incident_graph(incident_id: str) -> dict:
    """Start the investigation, or continue it from the last checkpoint after a crash."""
    with span("incident.investigate", incident_id=incident_id), checkpointed_graph(incident_id) as graph:
        cfg = _config(incident_id)
        snapshot = graph.get_state(cfg)
        if snapshot.next:
            if any(t.interrupts for t in snapshot.tasks):
                return {**_summary(incident_id), "note": "waiting for approval"}
            state = graph.invoke(None, cfg)  # resume after a crash mid-run
        elif snapshot.values:
            return {**_summary(incident_id), "note": "investigation already finished"}
        else:
            state = graph.invoke({"incident_id": incident_id}, cfg)
        return _summary(incident_id, state)


def resume_incident_graph(incident_id: str, approval_id: str, decision: str) -> dict:
    """Resume a paused graph with an approver's decision. Safe to deliver more than once."""
    with span("incident.resume", incident_id=incident_id, decision=decision), checkpointed_graph(incident_id) as graph:
        cfg = _config(incident_id)
        snapshot = graph.get_state(cfg)
        waiting = [i for t in snapshot.tasks for i in t.interrupts if i.value.get("approval_id") == approval_id]
        if not waiting:
            if snapshot.next and not any(t.interrupts for t in snapshot.tasks):
                state = graph.invoke(None, cfg)  # resumed earlier but crashed mid-execution: continue
                return _summary(incident_id, state)
            return {**_summary(incident_id), "note": "no pending interrupt for this approval (duplicate or stale)"}
        state = graph.invoke(Command(resume={"approval_id": approval_id, "decision": decision}), cfg)
        return _summary(incident_id, state)


def graph_state(incident_id: str) -> dict:
    with checkpointed_graph(incident_id) as graph:
        snap = graph.get_state(_config(incident_id))
        return {
            "next": list(snap.next),
            "values": snap.values,
            "interrupts": [i.value for t in snap.tasks for i in t.interrupts],
        }
