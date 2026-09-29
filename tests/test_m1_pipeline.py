"""Milestone 1 acceptance: healthy data reconciles, an injected fault demonstrably fails."""

import pytest

from datawarden.checks.registry import run_checks
from datawarden.faults import injector
from datawarden.oracle.reconciliation import source_truth
from datawarden.pipeline.runner import run_pipeline
from datawarden.warehouse.conn import connect

pytestmark = pytest.mark.integration


def _status(check_id):
    with connect("validator") as conn:
        return {r.check_id: r for r in run_checks(conn)}[check_id]


def test_healthy_pipeline_reconciles_to_source_truth(clean):
    with connect("validator") as conn:
        results = run_checks(conn)
        rows = conn.execute(
            "SELECT count(*) AS n, sum(net_revenue_paise) AS net FROM marts.mart_daily_revenue"
        ).fetchone()
    assert [r.check_id for r in results if r.status != "pass"] == []
    truth = source_truth()
    assert rows["n"] == 30
    assert rows["net"] == sum(d.net_revenue_paise for d in truth.daily.values())


def test_duplicate_payment_fault_fails_reconciliation(clean):
    injector.inject("duplicate_payments")
    report = run_pipeline(trigger="test")
    failing = {c.check_id for c in report.failing_checks()}
    assert "reconciliation.mart_daily_revenue" in failing
    assert "unique.fct_payments.payment_id" in failing
    truth = injector.load_state()["active"][0]["ground_truth"]
    recon = next(c for c in report.checks if c.check_id == "reconciliation.mart_daily_revenue")
    assert set(recon.observed["mismatched_dates"]) == set(truth["affected_dates"])
    assert all(d["net_difference_paise"] > 0 for d in recon.observed["detail"])  # inflated, not deflated


def test_ingestion_is_idempotent_and_raw_is_append_only(clean):
    with connect("pipeline", autocommit=True) as conn:
        before = conn.execute("SELECT count(*) AS n FROM raw.raw_payment_events").fetchone()["n"]
    report = run_pipeline(trigger="test")
    assert report.ingest.ingested == []
    with connect("pipeline", autocommit=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.raw_payment_events").fetchone()["n"] == before
        with pytest.raises(Exception, match="permission denied|append-only"):
            conn.execute("DELETE FROM raw.raw_payment_events WHERE true")


def test_late_events_need_bounded_replay(clean):
    from datetime import date

    injector.inject("late_events")
    run_pipeline(trigger="test")
    assert _status("reconciliation.mart_daily_revenue").status == "fail"
    with connect("validator") as conn:
        before = {
            r["business_date"]: r["net_revenue_paise"]
            for r in conn.execute("SELECT business_date, net_revenue_paise FROM marts.mart_daily_revenue")
        }
    report = run_pipeline(trigger="test", replay_window=(date(2026, 8, 20), date(2026, 8, 20)))
    assert report.failing_checks() == []
    with connect("validator") as conn:
        after = {
            r["business_date"]: r["net_revenue_paise"]
            for r in conn.execute("SELECT business_date, net_revenue_paise FROM marts.mart_daily_revenue")
        }
    changed = {d for d in after if after[d] != before.get(d)}
    assert changed == {date(2026, 8, 20)}


def test_schema_drift_rejects_whole_batch(clean):
    injector.inject("schema_drift_unregistered")
    report = run_pipeline(trigger="test")
    assert report.status == "failed"
    assert list(report.ingest.failed) == ["pay-20260831-v2"]
    assert "contract.raw_payment_events" in {c.check_id for c in report.failing_checks()}
