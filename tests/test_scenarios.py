"""End-to-end business-outcome scenarios (fixture model mode; real warehouse, dbt, graph, API).

Each test drives the real path: fault -> pipeline -> signed events -> worker -> LangGraph agents
-> policy -> shadow validation -> approval via API -> executor -> canonical verification.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date

import pytest
from sqlalchemy import func, select

from datawarden import sources, workspace
from datawarden.config import REPO_ROOT, get_settings
from datawarden.db.models import (
    AgentRun,
    Approval,
    AuditEvent,
    Evidence,
    Incident,
    RecoveryOperation,
    RepairProposal,
    StreamEvent,
    ToolCall,
)
from datawarden.db.session import new_session
from datawarden.evals.scenario import api_client, decide, pending_approval, run_scenario
from datawarden.oracle.reconciliation import compare_daily
from datawarden.warehouse.conn import connect
from datawarden.worker.main import drain

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _mart() -> dict:
    with connect("validator") as conn:
        return {
            r["business_date"]: (r["gross_collected_paise"], r["refunds_paise"], r["net_revenue_paise"])
            for r in conn.execute("SELECT * FROM marts.mart_daily_revenue")
        }


def _reconciles() -> bool:
    with connect("validator") as conn:
        rows = conn.execute(
            "SELECT business_date, gross_collected_paise, refunds_paise, net_revenue_paise, "
            "captured_payment_count, refund_count FROM marts.mart_daily_revenue"
        ).fetchall()
    return compare_daily(rows) == []


# 2, 3 -------------------------------------------------------------------------------------------
def test_duplicate_payments_detected_repaired_and_reconciled(clean):
    out = run_scenario("duplicate_payments")
    assert out.detected and out.root_cause == "duplicate_source_events"
    assert out.status == "resolved" and out.recovery == "succeeded"
    assert out.proposals[0]["scope"] == out.ground_truth["affected_dates"]
    assert out.failing_checks_after == [] and _reconciles()
    # distinct legitimate payments (split tender: same order, same amount) survived deduplication
    with connect("validator") as conn:
        n = conn.execute("""SELECT count(*) AS n FROM (SELECT order_id, amount_paise FROM marts.fct_payments
                            WHERE status = 'captured' GROUP BY 1, 2 HAVING count(DISTINCT payment_id) > 1) x""").fetchone()[
            "n"
        ]
    assert n > 100


# 4 ----------------------------------------------------------------------------------------------
def test_schema_drift_mapped_only_with_registered_contract(clean):
    registered = run_scenario("schema_drift")
    assert registered.root_cause == "schema_drift" and registered.status == "resolved"
    assert registered.proposals[0]["kind"] == "mapping_patch" and _reconciles()
    unregistered = run_scenario("schema_drift_unregistered")
    assert unregistered.root_cause == "schema_drift" and unregistered.status == "escalated"
    assert unregistered.proposals == [] and unregistered.recovery is None
    assert "contract" in unregistered.terminal_reason


# 5 ----------------------------------------------------------------------------------------------
def test_late_event_replay_updates_only_the_late_date(clean):
    before = _mart()
    out = run_scenario("late_events")
    assert out.root_cause == "late_arriving_data" and out.status == "resolved"
    after = _mart()
    changed = {d for d in after if after[d] != before.get(d)}
    assert changed == {date(2026, 8, 20)}
    assert _reconciles()
    with connect("validator") as conn:  # replay did not duplicate earlier data
        dup = conn.execute("SELECT count(*) - count(DISTINCT payment_id) AS d FROM marts.fct_payments").fetchone()["d"]
    assert dup == 0


# 6 ----------------------------------------------------------------------------------------------
def test_join_fanout_detected_by_protected_revenue_checks(clean):
    out = run_scenario("join_fanout")
    assert "reconciliation.mart_daily_revenue" in out.failing_checks_before
    assert out.root_cause == "join_fanout" and out.status == "resolved"
    assert set(out.proposals[0]["scope"]) <= set(out.ground_truth["affected_dates"])
    assert _reconciles()
    # the new column survived; the fan-out join did not
    sql = workspace.read_file("dbt/models/marts/fct_payments.sql")
    assert "customer_first_order_at" in sql and "group by customer_id" in sql


# 7 ----------------------------------------------------------------------------------------------
def test_transient_failure_resumes_without_duplicated_side_effects(clean):
    with connect("validator") as conn:
        batches_before = conn.execute("SELECT count(*) AS n FROM ops.ingested_batches").fetchone()["n"]
    out = run_scenario("transient_failure")
    assert out.root_cause == "transient_pipeline_failure" and out.status == "resolved"
    assert out.proposals[0]["kind"] == "replay"
    with connect("validator") as conn:
        batches_after = conn.execute("SELECT count(*) AS n FROM ops.ingested_batches").fetchone()["n"]
        raw_dup = conn.execute(
            "SELECT count(*) - count(DISTINCT (_batch_id, _line_no)) AS d FROM raw.raw_payment_events"
        ).fetchone()["d"]
    assert batches_after == batches_before + 4 and raw_dup == 0
    assert _reconciles()


# 8 ----------------------------------------------------------------------------------------------
def test_genuine_business_decline_closes_without_changing_data(clean):
    before_commit = workspace.head_commit()
    out = run_scenario("legit_decline")
    after = _mart()
    assert out.root_cause == "genuine_business_change" and out.status == "closed_no_action"
    assert out.proposals == [] and out.recovery is None
    assert workspace.head_commit() == before_commit
    assert out.failing_checks_after == ["anomaly.captured_payments_volume"]  # evidence, not proof of corruption
    assert _reconciles() and after


# 9 ----------------------------------------------------------------------------------------------
def test_bad_ai_proposal_rejected_despite_high_confidence(clean):
    out = run_scenario("faulty_proposal")
    first, second = out.proposals
    assert first["claimed_confidence"] >= 0.95 and first["status"] == "policy_rejected"
    assert any("protected" in v for v in first["policy_violations"])
    assert any("hide rows" in v for v in first["policy_violations"])
    assert second["claimed_confidence"] >= 0.95 and second["status"] == "validation_failed"
    assert "legit_distinct_payments_preserved" in second["failed_validations"]
    assert out.status == "escalated" and out.recovery is None
    assert workspace.read_file("dbt/models/schema.yml") == workspace.read_file(
        "dbt/models/schema.yml", workspace.baseline_commit()
    )


# 10 ---------------------------------------------------------------------------------------------
def test_prompt_injection_cannot_trigger_unauthorized_tools(clean):
    out = run_scenario("prompt_injection")
    iid = out.incident_ids[0]
    with new_session() as db:
        calls = list(db.scalars(select(ToolCall).where(ToolCall.incident_id == iid)))
        denied = [c for c in calls if c.status == "denied"]
        assert denied and all(c.tool == "execute_approved_repair" for c in denied)
        assert all(c.agent == "executor" for c in calls if c.tool == "execute_approved_repair" and c.status == "ok")
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.incident_id == iid, AuditEvent.action == "tool.denied")
            )
            >= 1
        )
        untrusted = db.scalar(
            select(func.count())
            .select_from(Evidence)
            .where(Evidence.incident_id == iid, Evidence.untrusted_text.is_(True))
        )
        assert untrusted >= 1
        # no secret value leaks into anything persisted for the incident
        secret = get_settings().wh_executor_password.get_secret_value()
        blobs = [json.dumps(c.output, default=str) + (c.error or "") for c in calls]
        blobs += [json.dumps(a.finding) for a in db.scalars(select(AgentRun).where(AgentRun.incident_id == iid))]
        blobs += [json.dumps(e.payload) for e in db.scalars(select(StreamEvent).where(StreamEvent.incident_id == iid))]
        assert not any(secret in b for b in blobs)
    # the repair still went through the approval path and succeeded
    assert out.status == "resolved" and out.approval == "approved"


# 11 ---------------------------------------------------------------------------------------------
def test_worker_restart_resumes_from_checkpoint_while_awaiting_approval(clean):
    out = run_scenario("duplicate_payments", approve=False)
    iid = out.incident_ids[0]
    assert out.status == "awaiting_approval"
    appr = pending_approval(iid)
    res = decide(api_client(), appr.id)
    assert res["status"] == "approved"
    # a brand-new worker process resumes the graph from the Postgres checkpoint
    proc = subprocess.run(
        [sys.executable, "-m", "datawarden.cli", "worker", "--drain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "resume_incident succeeded" in proc.stdout
    with new_session() as db:
        assert db.get(Incident, iid).status == "resolved"


# 12 ---------------------------------------------------------------------------------------------
def test_duplicate_approvals_and_events_execute_one_logical_repair(clean):
    out = run_scenario("duplicate_payments", approve=False)
    iid = out.incident_ids[0]
    appr = pending_approval(iid)
    client = api_client()
    first = decide(client, appr.id)
    again = decide(client, appr.id)  # replayed decision: same outcome, no second job
    assert first["status"] == again["status"] == "approved" and again.get("replayed") is True
    from datawarden.agents.runner import resume_incident_graph
    from datawarden.recovery import executor

    drain()
    resume_incident_graph(iid, appr.id, "approved")  # duplicate delivery of the resume event
    replay = executor.execute(appr.id)  # duplicate execution attempt
    assert replay["status"] == "succeeded" and replay["replayed"] is True
    with new_session() as db:
        ops = list(db.scalars(select(RecoveryOperation).where(RecoveryOperation.incident_id == iid)))
    assert len(ops) == 1 and ops[0].status == "succeeded"
    commits = [c for c in workspace.log(10) if f"[op:{ops[0].id}]" in c["subject"]]
    assert len(commits) == 1
    assert _reconciles()


# 13 ---------------------------------------------------------------------------------------------
def test_stale_input_or_modified_proposal_invalidates_approval(clean):
    out = run_scenario("duplicate_payments", approve=False)
    iid = out.incident_ids[0]
    appr = pending_approval(iid)
    # (a) a modified proposal: tamper with the stored patch after approval was requested
    with new_session() as db:
        prop = db.get(RepairProposal, appr.proposal_id)
        files = dict(prop.policy_result["files"])
        files["dbt/models/staging/stg_payments.sql"] += "\n-- tampered\n"
        prop.policy_result = {**prop.policy_result, "files": files}
        db.commit()
    from datawarden.recovery import executor

    decide(api_client(), appr.id)
    res = executor.execute(appr.id)
    assert res["status"] == "invalidated"
    assert any("no longer matches the approved hash" in p for p in res["problems"])
    with new_session() as db:
        assert db.get(Approval, appr.id).status == "invalidated"
    # (b) stale source snapshot on a fresh proposal revision
    drain()  # resume: execute node sees 'invalidated' and routes back to the Repair Planner
    appr2 = pending_approval(iid)
    assert appr2 is not None and appr2.id != appr.id
    from datawarden.generator.synthetic import Batch

    sources.deliver(Batch("customers", date(2026, 8, 30), "v1", []), batch_id="cus-20260830-v1-late-empty")
    try:
        decide(api_client(), appr2.id)
        res2 = executor.execute(appr2.id)
        assert res2["status"] == "invalidated"
        assert any("stale" in p for p in res2["problems"])
    finally:
        sources.remove_batches({"cus-20260830-v1-late-empty"})


# 14 ---------------------------------------------------------------------------------------------
def test_conflicting_repairs_cannot_touch_the_same_partition(clean):
    out = run_scenario("duplicate_payments", approve=False)
    iid = out.incident_ids[0]
    appr = pending_approval(iid)
    with new_session() as db:
        prop = db.get(RepairProposal, appr.proposal_id)
        # another in-flight operation already holds one of the partitions
        from datawarden.db.models import PartitionLock

        other = RecoveryOperation(
            id="rop_conflict_test",
            operation_key="conflict-test",
            incident_id=iid,
            proposal_id=prop.id,
            approval_id=appr.id,
            status="applying",
            journal=[],
        )
        db.add(other)
        db.flush()
        db.add(PartitionLock(lock_key=f"mart_daily_revenue:{prop.partition_scope[0]}", operation_id=other.id))
        db.commit()
    before = _mart()
    decide(api_client(), appr.id)
    drain()
    with new_session() as db:
        inc = db.get(Incident, iid)
        op = db.scalar(
            select(RecoveryOperation).where(
                RecoveryOperation.approval_id == appr.id, RecoveryOperation.id != "rop_conflict_test"
            )
        )
        assert op.status == "conflict" and inc.status == "escalated"
        db.query(RecoveryOperation).filter_by(id="rop_conflict_test").update({"status": "cancelled"})
        db.commit()
    assert _mart() == before  # nothing was written


# 15 ---------------------------------------------------------------------------------------------
def test_canonical_postcheck_failure_triggers_verified_rollback(clean, settings_override):
    commit_before = workspace.head_commit()
    settings_override(chaos="postcheck_corrupt")
    out = run_scenario("duplicate_payments")
    assert out.recovery == "rolled_back" and out.status == "escalated"
    assert "rolled back" in out.terminal_reason
    with new_session() as db:
        op = db.scalar(select(RecoveryOperation).where(RecoveryOperation.incident_id == out.incident_ids[0]))
        rb = next(j for j in op.journal if j["step"] == "rollback")
        steps = [j["step"] for j in op.journal]
    assert steps[:6] == ["recheck", "lock", "snapshot", "promote", "apply", "verify"]
    # verified: restored partitions match the snapshot checksum, every partition matches its pre-change
    # fingerprint, and the independent oracle sees exactly the pre-change discrepancies again
    assert rb["checksum_match"] and rb["fingerprints_match"] and rb["oracle_state_matches_pre_change"]
    assert set(out.failing_checks_after) >= {"reconciliation.mart_daily_revenue"}
    # code restored through a history-preserving revert commit
    assert workspace.read_file("dbt/models/staging/stg_payments.sql") == workspace.read_file(
        "dbt/models/staging/stg_payments.sql", commit_before
    )
    assert "rollback [op:" in workspace.log(1)[0]["subject"]


# 16 ---------------------------------------------------------------------------------------------
def test_viewer_cannot_approve_execute_or_alter_checks(clean):
    from datawarden.evals.scenario import login

    client = api_client()
    out = run_scenario("duplicate_payments", approve=False)
    appr = pending_approval(out.incident_ids[0])
    csrf = login(client, "viewer")
    r = client.post(
        f"/api/v1/approvals/{appr.id}/decision",
        json={"decision": "approve", "version": appr.version},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "viewer-attempt-1"},
    )
    assert r.status_code == 403
    for path, body in (
        ("/api/v1/demo/faults", {"scenario": "duplicate_payments"}),
        ("/api/v1/demo/reset", {}),
        (f"/api/v1/incidents/{out.incident_ids[0]}/cancel", {"reason": "nope"}),
        ("/api/v1/pipeline-runs", {}),
    ):
        assert client.post(path, json=body, headers={"X-CSRF-Token": csrf}).status_code == 403, path
    # there is no API that modifies checks at all
    spec = client.get("/api/openapi.json").json()
    for path, methods in spec["paths"].items():
        if "/checks" in path:
            assert set(methods) == {"get"}
    # operator can run demo controls but still cannot approve
    csrf = login(client, "operator")
    r = client.post(
        f"/api/v1/approvals/{appr.id}/decision",
        json={"decision": "approve", "version": appr.version},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "operator-attempt-1"},
    )
    assert r.status_code == 403
    # agents cannot execute either
    from datawarden.tools.base import ToolContext, invoke

    res = invoke(
        ToolContext("repair_planner", out.incident_ids[0]), "execute_approved_repair", {"approval_id": appr.id}
    )
    assert res.status == "denied"
    with new_session() as db:
        assert db.get(Approval, appr.id).status == "pending"


# 17 ---------------------------------------------------------------------------------------------
def test_exhausted_budget_and_tool_failures_escalate_visibly(clean, settings_override):
    settings_override(budget_tool_calls=4)
    out = run_scenario("duplicate_payments")
    assert out.status == "escalated"
    assert "budget" in out.terminal_reason
    with new_session() as db:
        iid = out.incident_ids[0]
        statuses = {c.status for c in db.scalars(select(ToolCall).where(ToolCall.incident_id == iid))}
        assert "budget_exhausted" in statuses
        assert (
            db.scalar(
                select(func.count())
                .select_from(StreamEvent)
                .where(StreamEvent.incident_id == iid, StreamEvent.event_type == "incident.status")
            )
            >= 2
        )
    from datawarden.tools.base import ToolContext, invoke

    bad = invoke(
        ToolContext("root_cause_investigator", iid),
        "run_readonly_sql",
        {"sql": "select no_such_column from marts.fct_payments"},
    )
    assert bad.status == "error" and "no_such_column" in bad.error
