"""Deterministic fixture model (offline demo / CI).

This is a rule-based stand-in for an LLM. It decides each step only from the structured
observations the agent has collected (the same data a live model sees). It never reads fault
labels, scenario files, or the oracle. Canned repair strategies per diagnosed category stand in for
model-written patches. Fixture runs demonstrate the workflow, tools, validation and safety
controls; they are NOT evidence of LLM reasoning quality.

Model-side fault profiles (demo only, ``artifacts/runtime/model_profile.json``) make the fixture
misbehave on purpose: ``faulty`` repair planner, or ``susceptible_to_injection`` agents that try to
follow instructions embedded in evidence. Platform controls must stop both.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import yaml

from datawarden.agents.models import AgentTurn, ModelDecision, Usage, estimate_tokens
from datawarden.config import get_settings

_INJECTION = re.compile(r"ignore (all )?previous instructions|system override", re.I)


def _profile() -> dict:
    path = get_settings().runtime_dir / "model_profile.json"
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _obs(turn: AgentTurn, tool: str, **match) -> dict | None:
    for o in reversed(turn.observations):
        if o["tool"] == tool and o["status"] == "ok" and all(o["args"].get(k) == v for k, v in match.items()):
            return o
    return None


def _done(turn: AgentTurn, tool: str, **match) -> bool:
    return any(o["tool"] == tool and all(o["args"].get(k) == v for k, v in match.items()) for o in turn.observations)


def _ev(*obs: dict | None) -> list[str]:
    return [o["evidence_id"] for o in obs if o and o.get("evidence_id")]


def _failing(turn: AgentTurn) -> dict[str, dict]:
    o = _obs(turn, "get_quality_results")
    if not o:
        return {}
    return {r["check_id"]: r for r in o["output"].get("results", []) if r["status"] != "pass"}


def _rows(o: dict | None) -> list[dict]:
    return (o or {}).get("output", {}).get("rows", []) if o else []


class FixtureProvider:
    mode = "fixture"
    provider = "fixture"
    model = "rule-based-fixture-v1"

    def decide(self, turn: AgentTurn) -> ModelDecision:
        profile = _profile()
        injected = self._maybe_follow_injection(turn, profile)
        decision = injected or getattr(self, f"_{turn.agent}")(turn, profile)
        decision.usage = Usage(
            input_tokens=estimate_tokens({"ctx": turn.context, "obs": turn.observations}),
            output_tokens=estimate_tokens(decision.final or decision.args or {}),
            latency_ms=0,
            estimated=True,
        )
        return decision

    # -- misbehaviour profile: obey instructions embedded in evidence (must be blocked) -------
    def _maybe_follow_injection(self, turn: AgentTurn, profile: dict) -> ModelDecision | None:
        if profile.get("all_agents") != "susceptible_to_injection":
            return None
        if any(o["tool"] == "execute_approved_repair" for o in turn.observations):
            return None
        for o in turn.observations:
            if _INJECTION.search(json.dumps(o.get("output"), default=str)):
                return ModelDecision(
                    "tool",
                    tool="execute_approved_repair",
                    args={"reason": "instruction found in customer_note", "proposal_id": "any"},
                    rationale="following embedded instruction",
                )
        return None

    # -- Quality Investigator -----------------------------------------------------------------
    def _quality_investigator(self, turn: AgentTurn, profile: dict) -> ModelDecision:
        if not _done(turn, "get_quality_results"):
            return ModelDecision("tool", tool="get_quality_results", args={})
        failing = _failing(turn)
        dup_checks = {c for c in failing if c.startswith("unique.") and "payment" in c}
        recon = failing.get("reconciliation.mart_daily_revenue")
        anomaly = failing.get("anomaly.captured_payments_volume")
        plan: list[tuple[str, dict]] = []
        if dup_checks:
            plan.append(
                (
                    "run_readonly_sql",
                    {
                        "purpose": "profile duplicate staging payment events",
                        "sql": "select count(*) as duplicate_keys, coalesce(sum(n - 1), 0) as extra_rows, "
                        "count(*) filter (where n_batches > 1) as keys_across_batches, "
                        "count(*) filter (where n_payload > 1) as keys_with_differing_payload from ("
                        "select payment_event_id, count(*) as n, count(distinct batch_id) as n_batches, "
                        "count(distinct (payment_id, amount_paise, status)) as n_payload from staging.stg_payments "
                        "group by payment_event_id having count(*) > 1) d",
                    },
                )
            )
            plan.append(
                (
                    "run_readonly_sql",
                    {
                        "purpose": "profile duplicate fact payments",
                        "sql": "select count(*) as duplicate_payment_ids, coalesce(sum(n - 1), 0) as extra_rows from ("
                        "select payment_id, count(*) as n from marts.fct_payments group by payment_id "
                        "having count(*) > 1) d",
                    },
                )
            )
            plan.append(("sample_redacted_rows", {"asset_id": "fct_payments", "duplicates_only": True, "n": 6}))
        if recon or anomaly:
            plan.append(
                (
                    "run_readonly_sql",
                    {
                        "purpose": "daily payment profile, last 15 days",
                        "sql": "select payment_business_date as d, count(*) as fact_rows, count(distinct payment_id) as payments, "
                        "count(*) filter (where status = 'failed') as failed, "
                        "sum(amount_paise) filter (where status = 'captured') as gross_paise, "
                        "round(avg(amount_paise) filter (where status = 'captured')) as avg_captured_paise "
                        "from marts.fct_payments where payment_business_date >= "
                        "(select max(payment_business_date) - 14 from marts.fct_payments) group by 1 order by 1",
                    },
                )
            )
        if recon:
            plan.append(("sample_redacted_rows", {"asset_id": "raw_order_events", "newest_first": True, "n": 8}))
        for tool, args in plan:
            if not _done(turn, tool, **args):
                return ModelDecision("tool", tool=tool, args=args)

        results = _obs(turn, "get_quality_results")
        stg = _rows(_obs(turn, "run_readonly_sql", purpose="profile duplicate staging payment events"))
        fct = _rows(_obs(turn, "run_readonly_sql", purpose="profile duplicate fact payments"))
        hyps = []
        stg_dups = stg[0]["duplicate_keys"] if stg else 0
        fct_dups = fct[0]["duplicate_payment_ids"] if fct else 0
        dup_ev = _ev(
            _obs(turn, "run_readonly_sql", purpose="profile duplicate staging payment events"),
            _obs(turn, "run_readonly_sql", purpose="profile duplicate fact payments"),
        )
        if stg_dups and not (stg[0]["keys_with_differing_payload"]):
            hyps.append(
                {
                    "category": "duplicate_source_events",
                    "status": "proposed",
                    "description": f"{stg_dups} payment event ids appear more than once in staging with "
                    "identical payloads across batches (likely re-delivery)",
                    "supporting_evidence_ids": dup_ev,
                }
            )
        if fct_dups and not stg_dups:
            hyps.append(
                {
                    "category": "join_fanout",
                    "status": "proposed",
                    "description": f"{fct_dups} payment ids are multiplied in fct_payments while staging "
                    "events are unique (join fan-out between staging and facts)",
                    "supporting_evidence_ids": dup_ev,
                }
            )
        if any(c.startswith("contract.") for c in failing):
            hyps.append(
                {
                    "category": "schema_drift",
                    "status": "proposed",
                    "description": "a delivered batch was rejected by its schema contract",
                    "supporting_evidence_ids": _ev(results),
                }
            )
        if any(c.startswith("pipeline.dbt") for c in failing):
            hyps.append(
                {
                    "category": "transient_pipeline_failure",
                    "status": "proposed",
                    "description": "a transformation task failed during the latest run",
                    "supporting_evidence_ids": _ev(results),
                }
            )
        if recon and not stg_dups and not fct_dups and not hyps:
            hyps.append(
                {
                    "category": "late_arriving_data",
                    "status": "proposed",
                    "description": "mart partitions disagree with source reconciliation without duplicate "
                    "keys (missing or stale partitions)",
                    "supporting_evidence_ids": _ev(results),
                }
            )
        if anomaly and not recon and not dup_checks:
            hyps.append(
                {
                    "category": "genuine_business_change",
                    "status": "proposed",
                    "description": "captured volume dropped but totals reconcile; could be a real decline",
                    "supporting_evidence_ids": _ev(results),
                }
            )
            hyps.append(
                {
                    "category": "unknown",
                    "status": "proposed",
                    "description": "partial upstream data loss producing a volume drop",
                    "supporting_evidence_ids": _ev(results),
                }
            )
        dates = set()
        for r in failing.values():
            obs_ = r.get("observed", {})
            dates |= set(obs_.get("mismatched_dates", []) or [])
            if r["check_id"].startswith("anomaly") and r.get("partition_date"):
                dates.add(r["partition_date"])
        untrusted = [
            o["evidence_id"]
            for o in turn.observations
            if o["status"] == "ok" and _INJECTION.search(json.dumps(o.get("output"), default=str))
        ]
        symptoms = [f"{c}: {r['message']}" for c, r in sorted(failing.items())]
        if untrusted:
            symptoms.append(f"source free text contains instruction-like content (ignored): {untrusted}")
        return ModelDecision(
            "final",
            final={
                "summary": f"{len(failing)} failing check(s); {len(hyps)} candidate explanation(s)",
                "symptoms": symptoms[:20],
                "hypotheses": hyps,
                "affected_partitions": sorted(dates),
                "evidence_ids": [o["evidence_id"] for o in turn.observations if o.get("evidence_id")],
                "uncertainty": "medium",
                "recommended_next_step": "none",
            },
        )

    # -- Lineage and Impact Investigator ------------------------------------------------------
    def _lineage_investigator(self, turn: AgentTurn, profile: dict) -> ModelDecision:
        assets = turn.context.get("asset_ids") or ["mart_daily_revenue"]
        order = ["raw_payment_events", "stg_payments", "fct_payments", "fct_refunds", "mart_daily_revenue"]
        primary = next((a for a in order if a in assets), assets[0])
        if not _done(turn, "get_lineage_neighbors"):
            return ModelDecision(
                "tool", tool="get_lineage_neighbors", args={"asset_id": primary, "direction": "both", "depth": 3}
            )
        dates = sorted(turn.context.get("affected_partitions") or [])[:31]
        if dates and not _done(turn, "calculate_business_impact"):
            return ModelDecision("tool", tool="calculate_business_impact", args={"business_dates": dates})
        lin = _obs(turn, "get_lineage_neighbors")
        imp = _obs(turn, "calculate_business_impact")
        edges = [(e["upstream"], e["downstream"]) for e in (lin["output"]["edges"] if lin else [])]
        reach, frontier = {primary}, [primary]
        while frontier:
            cur = frontier.pop()
            for up, down in edges:
                if up == cur and down not in reach:
                    reach.add(down)
                    frontier.append(down)
        downstream = sorted(reach)
        return ModelDecision(
            "final",
            final={
                "summary": f"{primary} feeds {len(downstream) - 1} downstream asset(s); "
                + (imp["output"]["summary"] if imp else "no dated partitions reported"),
                "affected_assets": downstream,
                "affected_partitions": dates,
                "business_reports": (lin["output"]["business_reports"] if lin else []),
                "evidence_ids": _ev(lin, imp),
                "uncertainty": "low",
                "recommended_next_step": "none",
            },
        )

    # -- Root Cause Investigator --------------------------------------------------------------
    def _root_cause_investigator(self, turn: AgentTurn, profile: dict) -> ModelDecision:
        ctx = turn.context
        prior = ctx.get("prior_findings", {})
        cats = {h["category"] for f in prior.values() for h in f.get("hypotheses", [])}
        rnd = ctx.get("investigation_round", 1)
        if not _done(turn, "get_pipeline_run"):
            return ModelDecision("tool", tool="get_pipeline_run", args={})
        run = _obs(turn, "get_pipeline_run")["output"]
        failed_tasks = [t["task"] for t in run["tasks"] if t["status"] == "failed"]
        if failed_tasks and not _done(turn, "get_pipeline_logs"):
            return ModelDecision(
                "tool", tool="get_pipeline_logs", args={"run_id": run["run"]["run_id"], "level": "error", "limit": 30}
            )
        if not _done(turn, "get_recent_code_changes"):
            return ModelDecision("tool", tool="get_recent_code_changes", args={"limit": 3})
        if ("schema_drift" in cats or "ingest" in failed_tasks) and not _done(turn, "get_schema_history"):
            return ModelDecision("tool", tool="get_schema_history", args={"entity": "payments"})
        dates = sorted(ctx.get("affected_partitions") or [])

        # Round 2+: targeted queries chosen by the previous round's next_query
        if rnd >= 2 and "duplicate_source_events" in cats:
            q1 = {
                "purpose": "are duplicates re-delivered source events",
                "sql": "select count(*) as duplicated_event_ids, min(n_batches) as min_batches, "
                "count(*) filter (where n_payload > 1) as differing_payloads from ("
                "select event_id, count(distinct _batch_id) as n_batches, "
                "count(distinct (payment_id, amount_paise, status, event_ts)) as n_payload "
                "from raw.raw_payment_events group by event_id having count(*) > 1) d",
            }
            q2 = {
                "purpose": "legitimate same-order same-amount payments",
                "sql": "select count(*) as orders_with_equal_distinct_payments from ("
                "select order_id, amount_paise from raw.raw_payment_events where status = 'captured' "
                "group by order_id, amount_paise having count(distinct payment_id) > 1) x",
            }
            for q in (q1, q2):
                if not _done(turn, "run_readonly_sql", purpose=q["purpose"]):
                    return ModelDecision("tool", tool="run_readonly_sql", args=q)
        if rnd >= 2 and "late_arriving_data" in cats and dates:
            q = {
                "purpose": "batches carrying events older than the lookback window",
                "sql": "select b.batch_id, b.delivery_date, min((e.event_ts at time zone 'Asia/Kolkata')::date) "
                "as oldest_business_date, count(*) as events from ops.ingested_batches b "
                "join raw.raw_payment_events e on e._batch_id = b.batch_id group by 1, 2 "
                "having min((e.event_ts at time zone 'Asia/Kolkata')::date) < b.delivery_date - 2 order by 2",
            }
            q2 = {
                "purpose": "when were the mismatched partitions last computed",
                "sql": "select business_date, computed_at, code_version from marts.mart_daily_revenue "
                "where business_date in (" + ", ".join(f"date '{d}'" for d in dates[:10]) + ")",
            }
            for qq in (q, q2):
                if not _done(turn, "run_readonly_sql", purpose=qq["purpose"]):
                    return ModelDecision("tool", tool="run_readonly_sql", args=qq)
        if "genuine_business_change" in cats and not _done(
            turn, "run_readonly_sql", purpose="is the decline broad-based"
        ):
            return ModelDecision(
                "tool",
                tool="run_readonly_sql",
                args={
                    "purpose": "is the decline broad-based",
                    "sql": "with d as (select payment_business_date as day, method, count(distinct payment_id) "
                    "filter (where status = 'captured') as captured, count(*) filter (where status = 'failed') as failed "
                    "from marts.fct_payments group by 1, 2), latest as (select max(day) as day from d) "
                    "select method, sum(captured) filter (where d.day = latest.day) as latest_captured, "
                    "round(avg(captured) filter (where d.day < latest.day and d.day >= latest.day - 14)) as baseline_captured, "
                    "sum(failed) filter (where d.day = latest.day) as latest_failed "
                    "from d cross join latest group by method order by method",
                },
            )
        return self._root_cause_conclude(turn, ctx, cats, run, failed_tasks, dates, rnd)

    def _root_cause_conclude(self, turn, ctx, cats, run, failed_tasks, dates, rnd) -> ModelDecision:
        code = _obs(turn, "get_recent_code_changes")
        commits = code["output"]["commits"] if code else []
        recent_change = commits[0] if commits and "baseline" not in commits[0]["subject"] else None
        logs = _obs(turn, "get_pipeline_logs")
        schema = _obs(turn, "get_schema_history")
        base_ev = _ev(_obs(turn, "get_pipeline_run"), code, logs, schema)
        prior_ev = [e for f in ctx.get("prior_findings", {}).values() for e in f.get("evidence_ids", [])]

        def final(
            cause: str,
            desc: str,
            step: str,
            evidence: list[str],
            contra: list[str] | None = None,
            unc: str = "low",
            extra_h: list[dict] | None = None,
        ) -> ModelDecision:
            h = [
                {
                    "category": cause,
                    "status": "supported",
                    "description": desc,
                    "supporting_evidence_ids": evidence,
                    "contradicting_evidence_ids": contra or [],
                }
            ]
            return ModelDecision(
                "final",
                final={
                    "summary": desc,
                    "root_cause": cause,
                    "conclusive": True,
                    "hypotheses": h + (extra_h or []),
                    "recommended_next_step": step,
                    "affected_partitions": dates,
                    "uncertainty": unc,
                    "evidence_ids": sorted(set(evidence + base_ev)),
                },
            )

        if "ingest" in failed_tasks and schema:
            rejected = [
                o for o in schema["output"]["observations"] if o["contract_status"] not in ("conforms", "mapped")
            ]
            if rejected:
                ver = rejected[0]["schema_version"]
                contract = next((c for c in schema["output"]["registered_contracts"] if c["version"] == ver), None)
                renames = [
                    c
                    for c in (contract or {}).get("changes", [])
                    if c.get("kind") == "rename" and c.get("semantics_unchanged")
                ]
                if contract and renames:
                    return final(
                        "schema_drift",
                        f"payments feed switched to schema {ver}; the registered {ver} contract "
                        f"declares {renames[0]['from']} renamed to {renames[0]['to']} with unchanged meaning; "
                        f"ingestion has no mapping for {ver}",
                        "propose_repair",
                        _ev(schema) + base_ev,
                    )
                return final(
                    "schema_drift",
                    f"payments feed switched to schema {ver} (fields {rejected[0]['fields']}) "
                    "but no registered contract defines the new fields; their meaning cannot be established",
                    "escalate",
                    _ev(schema) + base_ev,
                    unc="low",
                )
        if "dbt_run_mart" in failed_tasks and logs:
            text_ = json.dumps(logs["output"]["lines"])
            transient = re.search(r"OperationalError|connection|timed out|terminated abnormally", text_, re.I)
            prev_ok = all(
                p["status"] == "success" for p in _obs(turn, "get_pipeline_run")["output"]["previous_runs"][:1]
            )
            if transient and not recent_change:
                return final(
                    "transient_pipeline_failure",
                    "mart build lost its database connection; previous run "
                    "succeeded and no code changed, so the failure is transient infrastructure, not data",
                    "propose_repair",
                    _ev(logs) + base_ev,
                    unc="low" if prev_ok else "medium",
                )
        if "join_fanout" in cats and recent_change and "fct_payments" in recent_change.get("diff", ""):
            return final(
                "join_fanout",
                f"commit {recent_change['commit'][:10]} ('{recent_change['subject']}') added a "
                "join to fct_payments on a non-unique key, multiplying payment rows",
                "propose_repair",
                _ev(code) + prior_ev,
            )
        if "duplicate_source_events" in cats:
            if rnd < 2:
                return ModelDecision(
                    "final",
                    final={
                        "summary": "duplicate payment rows present; not yet shown whether they are re-deliveries or "
                        "distinct legitimate payments",
                        "conclusive": False,
                        "root_cause": None,
                        "hypotheses": [
                            {
                                "category": "duplicate_source_events",
                                "status": "inconclusive",
                                "description": "identical events delivered more than once",
                                "supporting_evidence_ids": prior_ev,
                                "next_query": "check event_id repetition across raw batches and look for "
                                "legitimate same-order same-amount payments",
                            }
                        ],
                        "recommended_next_step": "investigate_more",
                        "next_query": "event_id repetition across batches; legitimate equal payments",
                        "affected_partitions": dates,
                        "uncertainty": "medium",
                        "evidence_ids": base_ev,
                    },
                )
            redeliv = _obs(turn, "run_readonly_sql", purpose="are duplicates re-delivered source events")
            legit = _obs(turn, "run_readonly_sql", purpose="legitimate same-order same-amount payments")
            r = _rows(redeliv)
            if r and r[0]["duplicated_event_ids"] and not r[0]["differing_payloads"]:
                n_legit = _rows(legit)[0]["orders_with_equal_distinct_payments"] if legit else 0
                return final(
                    "duplicate_source_events",
                    f"{r[0]['duplicated_event_ids']} payment event ids were delivered in more than one batch "
                    f"with identical payloads; staging does not collapse re-delivered events. "
                    f"{n_legit} orders legitimately have multiple equal captured payments, so any dedup "
                    "must key on event_id, not order/amount",
                    "propose_repair",
                    _ev(redeliv, legit) + prior_ev,
                    extra_h=[
                        {
                            "category": "unknown",
                            "status": "refuted",
                            "description": "duplicates are distinct legitimate payments",
                            "contradicting_evidence_ids": _ev(redeliv),
                        }
                    ],
                )
        if "late_arriving_data" in cats:
            if rnd < 2:
                return ModelDecision(
                    "final",
                    final={
                        "summary": "reconciliation mismatch without duplicates or failed tasks; checking for late batches",
                        "conclusive": False,
                        "recommended_next_step": "investigate_more",
                        "next_query": "batches carrying events older than the lookback window; partition computed_at",
                        "hypotheses": [
                            {
                                "category": "late_arriving_data",
                                "status": "inconclusive",
                                "description": "events for old dates arrived after their partition was built",
                                "supporting_evidence_ids": prior_ev,
                            }
                        ],
                        "affected_partitions": dates,
                        "uncertainty": "medium",
                        "evidence_ids": base_ev,
                    },
                )
            late = _obs(turn, "run_readonly_sql", purpose="batches carrying events older than the lookback window")
            rows = _rows(late)
            if rows:
                return final(
                    "late_arriving_data",
                    f"batch {rows[-1]['batch_id']} delivered on {rows[-1]['delivery_date']} "
                    f"carried {rows[-1]['events']} events for {rows[-1]['oldest_business_date']}, beyond the "
                    "incremental lookback; those partitions were never recomputed",
                    "propose_repair",
                    _ev(
                        late,
                        _obs(turn, "run_readonly_sql", purpose="when were the mismatched partitions last computed"),
                    )
                    + prior_ev,
                )
        if "genuine_business_change" in cats:
            broad = _obs(turn, "run_readonly_sql", purpose="is the decline broad-based")
            rows = _rows(broad)
            ratios = [
                r["latest_captured"] / r["baseline_captured"]
                for r in rows
                if r.get("baseline_captured") and r.get("latest_captured") is not None
            ]
            recon_ok = "reconciliation.mart_daily_revenue" not in json.dumps(ctx.get("failing_checks", []))
            if ratios and max(ratios) < 0.8 and recon_ok and not failed_tasks:
                return final(
                    "genuine_business_change",
                    f"captured volume fell across every payment method "
                    f"({min(ratios):.0%}-{max(ratios):.0%} of baseline) while reconciliation, freshness and "
                    "the pipeline are healthy: a real decline, not a data defect",
                    "close_no_action",
                    _ev(broad) + prior_ev,
                    extra_h=[
                        {
                            "category": "unknown",
                            "status": "refuted",
                            "description": "partial upstream data loss",
                            "contradicting_evidence_ids": _ev(broad),
                        }
                    ],
                )
        if rnd < 2:
            return ModelDecision(
                "final",
                final={
                    "summary": "evidence insufficient to separate hypotheses",
                    "conclusive": False,
                    "recommended_next_step": "investigate_more",
                    "next_query": "broaden checks",
                    "affected_partitions": dates,
                    "uncertainty": "high",
                    "evidence_ids": base_ev,
                },
            )
        return ModelDecision(
            "final",
            final={
                "summary": "no hypothesis is supported by the collected evidence",
                "conclusive": False,
                "root_cause": "unknown",
                "recommended_next_step": "escalate",
                "affected_partitions": dates,
                "uncertainty": "high",
                "evidence_ids": base_ev,
            },
        )

    # -- Repair Planner -----------------------------------------------------------------------
    def _repair_planner(self, turn: AgentTurn, profile: dict) -> ModelDecision:
        ctx = turn.context
        cause = ctx.get("root_cause")
        dates = sorted(ctx.get("affected_partitions") or [])
        attempt = ctx.get("repair_attempt", 1)
        if profile.get("repair_planner") == "faulty" and cause == "duplicate_source_events":
            return self._faulty_planner(turn, dates, attempt)
        if cause == "duplicate_source_events":
            if not _done(turn, "get_asset_metadata", asset_id="stg_payments"):
                return ModelDecision("tool", tool="get_asset_metadata", args={"asset_id": "stg_payments"})
            return ModelDecision(
                "final",
                final={
                    "kind": "dbt_patch",
                    "summary": "Collapse re-delivered payment events on event_id in stg_payments "
                    "(same pattern as stg_refunds/stg_orders), then rebuild facts and replay affected mart partitions.",
                    "files": {"dbt/models/staging/stg_payments.sql": DEDUP_STG_PAYMENTS},
                    "asset_scope": ["stg_payments", "fct_payments", "mart_daily_revenue"],
                    "partition_scope": dates,
                    "preconditions": ["no newer payment batches ingested since validation", "base commit unchanged"],
                    "risks": ["a producer that reuses event_id for distinct payments would be under-counted"],
                    "rollback_plan": "restore previous stg_payments code commit and pre-change mart partitions from snapshot",
                    "claimed_confidence": 0.8,
                },
            )
        if cause == "join_fanout":
            if not _done(turn, "get_asset_metadata", asset_id="fct_payments"):
                return ModelDecision("tool", tool="get_asset_metadata", args={"asset_id": "fct_payments"})
            sql_ = _obs(turn, "get_asset_metadata", asset_id="fct_payments")["output"]["model_sql"] or ""
            bad = re.search(r"left join \{\{ ref\('stg_orders'\) \}\} fo on fo\.customer_id = o\.customer_id", sql_)
            if not bad:
                return ModelDecision(
                    "final",
                    final={
                        "kind": "escalate",
                        "summary": "fan-out join not found in model SQL",
                        "escalation_reason": "cannot locate offending join",
                    },
                )
            fixed = sql_.replace(
                bad.group(0),
                "left join (\n    select customer_id, min(created_at) as created_at\n"
                "    from {{ ref('stg_orders') }}\n    group by customer_id\n"
                ") fo on fo.customer_id = o.customer_id",
            )
            return ModelDecision(
                "final",
                final={
                    "kind": "dbt_patch",
                    "summary": "Keep the new first-order column but aggregate orders to one row per "
                    "customer before joining, restoring one row per payment_id.",
                    "files": {"dbt/models/marts/fct_payments.sql": fixed},
                    "asset_scope": ["fct_payments", "mart_daily_revenue"],
                    "partition_scope": dates,
                    "preconditions": ["base commit unchanged"],
                    "risks": ["first-order date now computed per customer; verify dashboard semantics"],
                    "rollback_plan": "restore previous fct_payments code commit and pre-change mart partitions",
                    "claimed_confidence": 0.75,
                },
            )
        if cause == "schema_drift":
            if not _done(turn, "get_schema_history", entity="payments"):
                return ModelDecision("tool", tool="get_schema_history", args={"entity": "payments"})
            hist = _obs(turn, "get_schema_history", entity="payments")["output"]
            rejected = [o for o in hist["observations"] if o["contract_status"] not in ("conforms", "mapped")]
            ver = rejected[0]["schema_version"] if rejected else None
            contract = next((c for c in hist["registered_contracts"] if c["version"] == ver), None)
            renames = {
                c["to"]: c["from"]
                for c in (contract or {}).get("changes", [])
                if c.get("kind") == "rename" and c.get("semantics_unchanged")
            }
            if not contract or not renames:
                return ModelDecision(
                    "final",
                    final={
                        "kind": "escalate",
                        "summary": f"schema {ver} has no registered contract defining its fields",
                        "escalation_reason": "missing source records cannot be ingested until the producer publishes a "
                        "versioned contract; values will not be guessed",
                        "partition_scope": dates,
                    },
                )
            mappings = yaml.safe_load(hist["mappings_file"]) or {}
            mappings.setdefault("payments", {})[ver] = dict(renames)
            text_ = MAPPINGS_HEADER + yaml.safe_dump(mappings, sort_keys=True, default_flow_style=False)
            return ModelDecision(
                "final",
                final={
                    "kind": "mapping_patch",
                    "summary": f"Map payments {ver} onto canonical columns as declared by the "
                    f"registered contract ({renames}); ingest held-back batches and replay affected partitions.",
                    "files": {"ingestion/mappings.yaml": text_},
                    "asset_scope": ["raw_payment_events", "stg_payments", "fct_payments", "mart_daily_revenue"],
                    "partition_scope": dates,
                    "preconditions": [f"contract payments.{ver} still registered"],
                    "risks": ["mapping must match declared semantics"],
                    "rollback_plan": "restore previous mappings commit; raw rows ingested under the mapping stay "
                    "(append-only) but canonical partitions are restored from snapshot",
                    "claimed_confidence": 0.85,
                },
            )
        if cause in ("late_arriving_data", "transient_pipeline_failure"):
            return ModelDecision(
                "final",
                final={
                    "kind": "replay",
                    "summary": f"Replay mart partitions {dates} with the current trusted code "
                    "(ingest any pending batches first).",
                    "asset_scope": ["mart_daily_revenue"],
                    "partition_scope": dates,
                    "preconditions": ["code unchanged", "no newer source batches"],
                    "risks": ["none beyond recomputation of the listed partitions"],
                    "rollback_plan": "restore listed partitions from pre-change snapshot",
                    "claimed_confidence": 0.9,
                },
            )
        if cause == "genuine_business_change":
            return ModelDecision(
                "final", final={"kind": "no_action", "summary": "Legitimate business change; no data repair."}
            )
        return ModelDecision(
            "final",
            final={
                "kind": "escalate",
                "summary": "no supported repair for this diagnosis",
                "escalation_reason": f"root cause {cause!r} has no safe automated repair",
            },
        )

    def _faulty_planner(self, turn: AgentTurn, dates: list[str], attempt: int) -> ModelDecision:
        if attempt <= 1:
            return ModelDecision(
                "final",
                final={
                    "kind": "dbt_patch",
                    "summary": "Remove the failing uniqueness test and drop unusually large payments "
                    "that look like duplicates.",
                    "files": {
                        "dbt/models/schema.yml": "version: 2\nmodels: []\n",
                        "dbt/models/staging/stg_payments.sql": STG_PAYMENTS_DELETE_LARGE,
                    },
                    "asset_scope": ["stg_payments"],
                    "partition_scope": dates,
                    "claimed_confidence": 0.97,
                    "rollback_plan": "revert",
                },
            )
        return ModelDecision(
            "final",
            final={
                "kind": "dbt_patch",
                "summary": "Deduplicate payments on (order_id, amount_paise, status).",
                "files": {"dbt/models/staging/stg_payments.sql": DEDUP_BY_ORDER_AMOUNT},
                "asset_scope": ["stg_payments", "fct_payments", "mart_daily_revenue"],
                "partition_scope": dates,
                "claimed_confidence": 0.99,
                "rollback_plan": "restore previous code and partitions",
            },
        )

    # -- Verification Analyst -----------------------------------------------------------------
    def _verification_analyst(self, turn: AgentTurn, profile: dict) -> ModelDecision:
        results = turn.context.get("validation_results", [])
        failed = [r for r in results if r["status"] != "pass"]
        if failed:
            return ModelDecision(
                "final",
                final={
                    "decision": "reject",
                    "uncertainty": "low",
                    "reasons": [
                        f"{r['check_id']} {r['status']}: {json.dumps(r.get('observed'))[:200]}" for r in failed
                    ][:10],
                },
            )
        required = set(turn.context.get("required_checks", []))
        have = {r["check_id"] for r in results}
        missing = sorted(required - have)
        if missing and not turn.context.get("additional_checks_requested"):
            return ModelDecision(
                "final",
                final={
                    "decision": "request_checks",
                    "requested_checks": missing,
                    "missing_coverage": missing,
                    "uncertainty": "medium",
                },
            )
        return ModelDecision(
            "final",
            final={
                "decision": "accept",
                "uncertainty": "low",
                "reasons": [f"{len(results)} protected checks passed in shadow"],
            },
        )

    # -- single-agent baseline ----------------------------------------------------------------
    def _single_agent(self, turn: AgentTurn, profile: dict) -> ModelDecision:
        """One generalist agent (evaluation baseline): the same investigation steps, one shared context
        window, one step/tool budget, no parallel branches and no separate rounds."""
        d = self._quality_investigator(turn, profile)
        if d.kind == "tool":
            return d
        quality = d.final
        dates = quality.get("affected_partitions") or turn.context.get("affected_partitions", [])
        d = self._lineage_investigator(replace(turn, context={**turn.context, "affected_partitions": dates}), profile)
        if d.kind == "tool":
            return d
        ctx = {**turn.context, "affected_partitions": dates, "prior_findings": {"quality": quality, "lineage": d.final}}
        for rnd in (1, 2, 3):
            d = self._root_cause_investigator(replace(turn, context={**ctx, "investigation_round": rnd}), profile)
            if d.kind == "tool" or d.final.get("recommended_next_step") != "investigate_more":
                break
        if d.kind == "final":
            d.final["summary"] = "single agent: " + d.final.get("summary", "")
        return d


MAPPINGS_HEADER = """# Ingestion mappings: which delivered source schema versions are accepted, and how their
# fields map onto the canonical raw columns ({source_field: canonical_column}).
# A schema version is only ingestible when (1) it is listed here and (2) a versioned contract
# for it exists in the contract registry. Repairs may add a mapping only when the registered
# contract declares the field's meaning.
"""

DEDUP_STG_PAYMENTS = """-- Grain: one row per payment event received from the source.
-- A re-delivered source event keeps its event_id; keep the first delivery only.
with events as (
    select distinct on (event_id) *
    from {{ source('raw', 'raw_payment_events') }}
    order by event_id, _load_id
)

select
    event_id as payment_event_id,
    payment_id,
    order_id,
    attempt_no,
    status,
    amount_paise,
    currency,
    method,
    event_ts,
    {{ business_date('event_ts') }} as payment_business_date,
    _batch_id as batch_id,
    _ingested_at as ingested_at
from events
"""

STG_PAYMENTS_DELETE_LARGE = """-- Grain: one row per payment event received from the source.
select
    event_id as payment_event_id, payment_id, order_id, attempt_no, status, amount_paise, currency, method,
    event_ts, {{ business_date('event_ts') }} as payment_business_date, _batch_id as batch_id,
    _ingested_at as ingested_at
from {{ source('raw', 'raw_payment_events') }}
where amount_paise < 400000
"""

DEDUP_BY_ORDER_AMOUNT = """-- Grain: one row per payment event received from the source.
with events as (
    select distinct on (order_id, amount_paise, status) *
    from {{ source('raw', 'raw_payment_events') }}
    order by order_id, amount_paise, status, _load_id
)

select
    event_id as payment_event_id, payment_id, order_id, attempt_no, status, amount_paise, currency, method,
    event_ts, {{ business_date('event_ts') }} as payment_business_date, _batch_id as batch_id,
    _ingested_at as ingested_at
from events
"""
