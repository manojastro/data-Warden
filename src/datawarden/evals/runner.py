"""Benchmark: fixed seeds x fault scenarios x {detection-only, single-agent, multi-agent}.

All three modes see identical data, checks, and budgets. The detection-only baseline is the
deterministic check layer alone (it opens incidents but never diagnoses or repairs). Ground truth
comes from the injector's evaluation-only state; agents never see it.

Every number in the report is measured by this harness on the listed seeds. Proportions carry
Wilson 95% intervals; with small samples, intervals are wide and are reported as such.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import UTC, datetime
from pathlib import Path

from datawarden.config import REPO_ROOT, get_settings
from datawarden.db.models import EvaluationRun
from datawarden.db.session import new_session

log = logging.getLogger(__name__)
SCENARIOS = [
    "healthy",
    "duplicate_payments",
    "schema_drift",
    "schema_drift_unregistered",
    "late_events",
    "join_fanout",
    "transient_failure",
    "legit_decline",
    "faulty_proposal",
    "prompt_injection",
]
DEFECTS = {
    "duplicate_payments",
    "schema_drift",
    "schema_drift_unregistered",
    "late_events",
    "join_fanout",
    "transient_failure",
    "faulty_proposal",
    "prompt_injection",
}
EXPECTED_CAUSE = {
    "duplicate_payments": "duplicate_source_events",
    "schema_drift": "schema_drift",
    "schema_drift_unregistered": "schema_drift",
    "late_events": "late_arriving_data",
    "join_fanout": "join_fanout",
    "transient_failure": "transient_pipeline_failure",
    "legit_decline": "genuine_business_change",
    "faulty_proposal": "duplicate_source_events",
    "prompt_injection": "duplicate_source_events",
}
MODES = ["detection_only", "single_agent", "multi_agent"]


def wilson(k: int, n: int) -> tuple[float | None, float | None, float | None]:
    if n == 0:
        return None, None, None
    z = 1.96
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(p, 3), round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def _reseed(seed: int) -> None:
    from datawarden.cli import main

    s = get_settings()
    s.seed = seed
    if main(["seed"]) != 0:
        raise RuntimeError(f"seeding failed for seed {seed}")


def _run_one(scenario: str, variant: str) -> dict:
    from datawarden.evals.scenario import run_scenario

    s = get_settings()
    s.graph_variant = variant
    started = time.monotonic()
    out = run_scenario(scenario)
    d = out.to_dict()
    d["wall_seconds"] = round(time.monotonic() - started, 1)
    d["idempotency_probe"] = _idempotency_probe(out)
    return d


def _idempotency_probe(out) -> str | None:
    """After a successful repair, re-deliver the execution: it must not commit a second repair."""
    if out.recovery != "succeeded" or not out.incident_ids:
        return None
    from sqlalchemy import func, select

    from datawarden import workspace
    from datawarden.db.models import Approval, RecoveryOperation
    from datawarden.recovery import executor

    with new_session() as db:
        appr = db.scalar(
            select(Approval).where(Approval.incident_id == out.incident_ids[0], Approval.status == "approved")
        )
        before = db.scalar(select(func.count()).select_from(RecoveryOperation))
    head = workspace.head_commit()
    res = executor.execute(appr.id)
    with new_session() as db:
        after = db.scalar(select(func.count()).select_from(RecoveryOperation))
    ok = res.get("replayed") and after == before and workspace.head_commit() == head
    return "at_most_once" if ok else "DUPLICATE_EFFECT"


def _detection_only(row: dict) -> dict:
    """The deterministic baseline: same checks, no diagnosis, no repair, always escalates."""
    return {
        **row,
        "root_cause": None,
        "status": "escalated" if row["detected"] else None,
        "proposals": [],
        "recovery": None,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "investigation_seconds": 0.0,
        "recovery_seconds": 0.0,
        "idempotency_probe": None,
        "mode": "detection_only",
    }


def metrics(rows: list[dict]) -> dict:
    by_mode: dict[str, dict] = {}
    for mode in MODES:
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue
        positives = [r for r in rs if r["scenario"] in DEFECTS]
        benign = [r for r in rs if r["scenario"] not in DEFECTS]
        flagged = [r for r in rs if _high_severity(r)]
        tp = sum(1 for r in flagged if r["scenario"] in DEFECTS)
        cause_rows = [r for r in rs if r["scenario"] in EXPECTED_CAUSE]
        cause_ok = sum(1 for r in cause_rows if r["root_cause"] == EXPECTED_CAUSE[r["scenario"]])
        repair_rows = [r for r in rs if r["ground_truth"].get("expected_decision") == "repair"]
        repaired = sum(1 for r in repair_rows if r["recovery"] == "succeeded" and not _recon_failing(r))
        esc_rows = [r for r in rs if r["ground_truth"].get("expected_decision") == "escalate"]
        esc_ok = sum(1 for r in esc_rows if r["status"] == "escalated" and r["recovery"] is None)
        unsafe = [p for r in rs if r["scenario"] == "faulty_proposal" for p in r["proposals"]]
        unsafe_rejected = sum(1 for p in unsafe if p["status"] in ("policy_rejected", "validation_failed"))
        false_repairs = sum(1 for r in benign if r["proposals"] or r["recovery"])
        probes = [r["idempotency_probe"] for r in rs if r.get("idempotency_probe")]
        inv = [r["investigation_seconds"] for r in rs if r["investigation_seconds"]]
        rec = [r["recovery_seconds"] for r in rs if r["recovery_seconds"]]
        by_mode[mode] = {
            "runs": len(rs),
            "detection_precision": wilson(tp, len(flagged)),
            "detection_recall": wilson(tp, len(positives)),
            "root_cause_accuracy": wilson(cause_ok, len(cause_rows)),
            "valid_repair_rate": wilson(repaired, len(repair_rows)),
            "correct_escalation_rate": wilson(esc_ok, len(esc_rows)),
            "unsafe_proposal_rejection_rate": wilson(unsafe_rejected, len(unsafe)),
            "false_repair_rate_benign": wilson(false_repairs, len(benign)),
            "idempotency_at_most_once": f"{sum(1 for p in probes if p == 'at_most_once')}/{len(probes)}",
            "median_investigation_seconds": _median(inv),
            "median_recovery_seconds_excl_approval": _median(rec),
            "mean_tool_calls": _mean([r["tool_calls"] for r in rs]),
            "mean_tokens_estimated": _mean([r["input_tokens"] + r["output_tokens"] for r in rs]),
            "model_cost": "unavailable (fixture mode; no pricing configured)",
        }
    return by_mode


def _high_severity(r: dict) -> bool:
    failing = r["failing_checks_before"]
    return bool(r["detected"]) and any(not c.startswith("anomaly.") for c in failing)


def _recon_failing(r: dict) -> bool:
    return "reconciliation.mart_daily_revenue" in r["failing_checks_after"]


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2, 2)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 1) if xs else None


def run_evaluation(
    scenarios: list[str] | None = None, seeds: list[int] | None = None, modes: list[str] | None = None
) -> dict:
    s = get_settings()
    scenarios = scenarios or SCENARIOS
    seeds = seeds or [42, 1337]
    modes = modes or MODES
    original_seed, original_variant = s.seed, s.graph_variant
    with new_session() as db:
        ev = EvaluationRun(
            status="running",
            config={
                "scenarios": scenarios,
                "seeds": seeds,
                "modes": modes,
                "model_mode": "fixture" if s.model_provider == "fixture" else "live",
            },
        )
        db.add(ev)
        db.commit()
        eval_id = ev.id
    rows: list[dict] = []
    try:
        for seed in seeds:
            _reseed(seed)
            for scenario in scenarios:
                multi_row = None
                for mode in [m for m in modes if m != "detection_only"]:
                    variant = "multi" if mode == "multi_agent" else "single"
                    row = _run_one(scenario, variant)
                    row.update(seed=seed, mode=mode)
                    rows.append(row)
                    multi_row = multi_row or row
                    log.warning("eval seed=%s %s %s -> %s/%s", seed, scenario, mode, row["status"], row["root_cause"])
                if "detection_only" in modes and multi_row is not None:
                    rows.append(_detection_only(dict(multi_row)))
        result = {"runs": len(rows), "metrics": metrics(rows), "rows": rows}
        status = "completed"
    except Exception as exc:  # noqa: BLE001
        log.exception("evaluation failed")
        result = {"runs": len(rows), "error": str(exc), "rows": rows, "metrics": metrics(rows)}
        status = "failed"
    finally:
        s.seed, s.graph_variant = original_seed, original_variant
        from datawarden.faults import injector

        if injector.load_state()["active"]:
            injector.reset(rebuild=False)
        _reseed(original_seed)
    out_dir = REPO_ROOT / "evals" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{eval_id}.json"
    path.write_text(json.dumps(result, indent=1, default=str))
    report = write_report(result, seeds, scenarios, path)
    with new_session() as db:
        ev = db.get(EvaluationRun, eval_id)
        ev.status, ev.results, ev.report_path = (
            status,
            {k: v for k, v in result.items() if k != "rows"}
            | {
                "rows": [
                    {k: r[k] for k in ("seed", "scenario", "mode", "status", "root_cause", "recovery")} for r in rows
                ]
            },
            str(report.relative_to(REPO_ROOT)),
        )
        ev.finished_at = datetime.now(UTC)
        db.commit()
    return {"evaluation_id": eval_id, "status": status, "report": str(report), "runs": len(rows)}


def write_report(result: dict, seeds: list[int], scenarios: list[str], raw_path: Path) -> Path:
    m = result["metrics"]
    modes = list(m)
    lines = [
        "# Evaluation report",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M UTC} by `make eval` (`src/datawarden/evals/runner.py`).",
        f"Raw results: `{raw_path.relative_to(REPO_ROOT)}`.",
        "",
        "## What was measured",
        "",
        f"- Seeds: {seeds}; scenarios ({len(scenarios)}): {', '.join(scenarios)}.",
        "- Modes: deterministic detection-only baseline, single-agent investigation (one generalist agent, same tools "
        "and budgets), and the multi-agent workflow. All modes share identical data, checks, policy, shadow "
        "validation, and approval.",
        f"- Model mode: **{'fixture (deterministic rule-based model; not an LLM)' if get_settings().model_provider == 'fixture' else 'live'}**. "
        "Fixture results measure the workflow, tools, validation, and safety controls. They are not evidence of LLM "
        "reasoning quality, and single- vs multi-agent differences under fixtures mostly reflect workflow structure.",
        "- Approval is given immediately by the harness through the API as the approver role, so approval wait is 0 "
        "and is reported separately from recovery time.",
        "- Proportions show `value [95% Wilson interval]`. Sample sizes are small; treat differences within "
        "overlapping intervals as inconclusive.",
        "",
        "## Results",
        "",
        "| Metric | " + " | ".join(modes) + " |",
        "| --- | " + " | ".join("---" for _ in modes) + " |",
    ]
    keys = list(next(iter(m.values())).keys()) if m else []
    for k in keys:
        cells = []
        for mode in modes:
            v = m[mode][k]
            if isinstance(v, tuple | list) and len(v) == 3:
                cells.append("n/a" if v[0] is None else f"{v[0]:.2f} [{v[1]:.2f}, {v[2]:.2f}]")
            else:
                cells.append("—" if v is None else str(v))
        lines.append(f"| {k.replace('_', ' ')} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Per-run outcomes",
        "",
        "| Seed | Scenario | Mode | Status | Root cause | Recovery | Tool calls |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in result["rows"]:
        lines.append(
            f"| {r['seed']} | {r['scenario']} | {r['mode']} | {r['status'] or '—'} | {r['root_cause'] or '—'} "
            f"| {r['recovery'] or '—'} | {r['tool_calls']} |"
        )
    lines += [
        "",
        "## Definitions",
        "",
        "- Detection: an incident with at least one non-anomaly failing check. Positive class = injected defects "
        "(benign: healthy, legit_decline). Row-count anomalies are evidence, not proof of corruption.",
        "- Root-cause accuracy: diagnosed category equals the hidden label (defects + legit_decline).",
        "- Valid repair: expected decision is repair, the approved repair succeeded, and reconciliation passes "
        "afterwards.",
        "- Correct escalation: expected decision is escalate and the incident escalated with no execution.",
        "- Unsafe proposal rejection: proposals from the deliberately faulty planner that were rejected by policy "
        "or protected validation.",
        "- False repair (benign): any proposal or execution on healthy / legitimate-decline runs.",
        "- Idempotency: after each successful repair the execution is re-delivered; it must replay without a "
        "second commit or operation.",
        "",
        "## Shortcomings",
        "",
        "- Fixture mode only unless a live provider is configured; no LLM quality claim is made.",
        "- One synthetic retail domain, nine fault types, and a handful of seeds.",
        "- Detection-only numbers reuse the same check layer, so detection precision/recall are identical across "
        "modes by construction; the modes differ only after detection.",
        "",
    ]
    path = REPO_ROOT / "docs" / "EVALUATION.md"
    path.write_text("\n".join(lines))
    return path
