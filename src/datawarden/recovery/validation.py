"""Protected shadow validation. Trusted code; agents can request these checks but cannot edit them,
their thresholds, or their expected values. Expected values come from the independent oracle
(recomputed from immutable source fixtures), never from the repair under test.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from datetime import date

from psycopg import sql

from datawarden.contracts.agents import ValidationResult
from datawarden.oracle import reconciliation
from datawarden.recovery.policy import MAPPINGS_FILE, MODEL_FILE
from datawarden.warehouse.conn import connect

REQUIRED_COLUMNS = {
    "stg_payments": ["payment_event_id", "payment_id", "order_id", "status", "amount_paise", "payment_business_date"],
    "fct_payments": ["payment_id", "order_id", "status", "amount_paise", "payment_business_date"],
    "fct_refunds": ["refund_id", "payment_id", "status", "amount_paise", "refund_business_date"],
    "fct_orders": ["order_id", "customer_id", "order_total_paise", "order_business_date"],
    "mart_daily_revenue": [
        "business_date",
        "gross_collected_paise",
        "refunds_paise",
        "net_revenue_paise",
        "captured_payment_count",
        "refund_count",
    ],
}
UNIQUE_KEYS = [
    ("stg_payments", "payment_event_id"),
    ("fct_payments", "payment_id"),
    ("fct_refunds", "refund_id"),
    ("fct_orders", "order_id"),
    ("mart_daily_revenue", "business_date"),
]
STANDARD_CHECKS = [
    "protected_paths_untouched",
    "dbt_build_succeeded",
    "dbt_tests_passed",
    "schema_contract",
    "unique_keys",
    "referential_integrity",
    "revenue_reconciliation",
    "partition_boundary",
    "row_counts",
    "legit_distinct_payments_preserved",
    "downstream_query",
]
MART_COLS = (
    "business_date, gross_collected_paise, refunds_paise, net_revenue_paise, captured_payment_count, refund_count"
)


def _mart(conn, schema: str) -> dict[date, dict]:
    rows = conn.execute(
        sql.SQL(f"SELECT {MART_COLS} FROM {{}}.mart_daily_revenue").format(sql.Identifier(schema))
    ).fetchall()
    return {r["business_date"]: dict(r) for r in rows}


def changed_files(code_dir) -> list[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=code_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    return sorted(line[3:].strip() for line in out.splitlines() if line.strip())


def run_validation(
    *,
    proposal_hash: str,
    kind: str,
    files: dict[str, str],
    partition_scope: list[str],
    shadow_schema: str,
    code_dir,
    input_snapshot: dict,
    build_ok: bool,
    build_detail: str,
    tests_ok: bool | None,
    tests_detail: str,
    only: list[str] | None = None,
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    want = set(only or STANDARD_CHECKS)

    def add(check_id: str, ok: bool, expected: dict, observed: dict, ref: str | None = None) -> None:
        results.append(
            ValidationResult(
                proposal_hash=proposal_hash,
                input_snapshot=input_snapshot,
                check_id=check_id,
                expected=expected,
                observed=observed,
                status="pass" if ok else "fail",
                artifact_ref=ref,
            )
        )

    if "protected_paths_untouched" in want:
        changed = changed_files(code_dir)
        allowed = [f for f in changed if MODEL_FILE.match(f) or (f == MAPPINGS_FILE and kind == "mapping_patch")]
        add(
            "protected_paths_untouched",
            changed == allowed and set(changed) <= set(files),
            {"allowed": "existing dbt model SQL, or ingestion mappings for mapping_patch"},
            {"changed_files": changed, "disallowed": sorted(set(changed) - set(allowed))},
        )
    if "dbt_build_succeeded" in want:
        add("dbt_build_succeeded", build_ok, {"build": "success"}, {"detail": build_detail[-600:]})
    if not build_ok:
        for cid in STANDARD_CHECKS:
            if cid in want and cid not in {r.check_id for r in results}:
                results.append(
                    ValidationResult(
                        proposal_hash=proposal_hash,
                        input_snapshot=input_snapshot,
                        check_id=cid,
                        status="error",
                        observed={"reason": "shadow build failed; check could not run"},
                    )
                )
        return results
    if "dbt_tests_passed" in want and tests_ok is not None:
        add(
            "dbt_tests_passed",
            tests_ok,
            {"dbt_tests": "all pass (tests are unmodified baseline tests)"},
            {"detail": tests_detail[-600:]},
        )

    truth = reconciliation.source_truth()
    scope = {date.fromisoformat(d) for d in partition_scope}
    with connect("validator") as conn:
        s = sql.Identifier(shadow_schema)
        if "schema_contract" in want:
            cols = conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s",
                (shadow_schema,),
            ).fetchall()
            have = defaultdict(set)
            for c in cols:
                have[c["table_name"]].add(c["column_name"])
            missing = {t: sorted(set(req) - have[t]) for t, req in REQUIRED_COLUMNS.items() if set(req) - have[t]}
            add("schema_contract", not missing, {"required_columns": REQUIRED_COLUMNS}, {"missing": missing})
        if "unique_keys" in want:
            dups = {}
            for table, key in UNIQUE_KEYS:
                n = conn.execute(
                    sql.SQL("SELECT count(*) - count(DISTINCT {k}) AS d FROM {s}.{t}").format(
                        k=sql.Identifier(key), s=s, t=sql.Identifier(table)
                    )
                ).fetchone()["d"]
                dups[f"{table}.{key}"] = n
            add("unique_keys", not any(dups.values()), {"duplicates": 0}, {"duplicates": dups})
        if "referential_integrity" in want:
            orphan_p = conn.execute(
                sql.SQL(
                    "SELECT count(*) AS n FROM {s}.fct_payments p WHERE NOT EXISTS "
                    "(SELECT 1 FROM {s}.fct_orders o WHERE o.order_id = p.order_id)"
                ).format(s=s)
            ).fetchone()["n"]
            orphan_r = conn.execute(
                sql.SQL(
                    "SELECT count(*) AS n FROM {s}.fct_refunds r WHERE NOT EXISTS "
                    "(SELECT 1 FROM {s}.fct_payments p WHERE p.payment_id = r.payment_id)"
                ).format(s=s)
            ).fetchone()["n"]
            add(
                "referential_integrity",
                orphan_p == 0 and orphan_r == 0,
                {"orphans": 0},
                {"payments_without_order": orphan_p, "refunds_without_payment": orphan_r},
            )
        shadow_mart = _mart(conn, shadow_schema)
        canonical_mart = _mart(conn, "marts")
        if "revenue_reconciliation" in want:
            diffs = reconciliation.compare_daily(list(shadow_mart.values()))
            dates = sorted({str(d.business_date) for d in diffs})
            add(
                "revenue_reconciliation",
                not diffs,
                {"source": "independent oracle", "mismatched_dates": []},
                {
                    "mismatched_dates": dates,
                    "fields": sorted({d.field for d in diffs})[:5],
                    "partitions_checked": len(shadow_mart),
                },
            )
        if "partition_boundary" in want:
            changed = sorted(
                d
                for d in set(shadow_mart) | set(canonical_mart)
                if _money(shadow_mart.get(d)) != _money(canonical_mart.get(d))
            )
            outside = [str(d) for d in changed if d not in scope]
            wrong_now = {d.business_date for d in reconciliation.compare_daily(list(canonical_mart.values()))}
            unfixed = [str(d) for d in wrong_now if d not in scope]
            add(
                "partition_boundary",
                not outside and not unfixed,
                {"changes_only_within": sorted(partition_scope), "all_wrong_partitions_in_scope": True},
                {
                    "changed_partitions": [str(d) for d in changed],
                    "changed_outside_scope": outside,
                    "wrong_partitions_outside_scope": sorted(unfixed),
                },
            )
        if "row_counts" in want:
            fct = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}.fct_payments").format(s)).fetchone()["n"]
            stg = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}.stg_payments").format(s)).fetchone()["n"]
            captured = {r["business_date"]: r["captured_payment_count"] for r in shadow_mart.values()}
            exp_captured = {d: t.captured_payment_count for d, t in truth.daily.items()}
            mismatch = sorted(
                str(d) for d in set(captured) | set(exp_captured) if captured.get(d, 0) != exp_captured.get(d, 0)
            )
            add(
                "row_counts",
                fct == truth.distinct_payment_ids and stg == truth.distinct_payment_event_ids and not mismatch,
                {
                    "fct_payments_rows": truth.distinct_payment_ids,
                    "stg_payments_rows": truth.distinct_payment_event_ids,
                },
                {"fct_payments_rows": fct, "stg_payments_rows": stg, "captured_count_mismatch_dates": mismatch[:20]},
            )
        if "legit_distinct_payments_preserved" in want:
            ids = {
                r["payment_id"]
                for r in conn.execute(
                    sql.SQL("SELECT DISTINCT payment_id FROM {}.fct_payments WHERE status = 'captured'").format(s)
                ).fetchall()
            }
            missing = truth.captured_payment_ids - ids
            extra = ids - truth.captured_payment_ids
            add(
                "legit_distinct_payments_preserved",
                not missing and not extra,
                {"captured_distinct_payments": len(truth.captured_payment_ids)},
                {
                    "captured_distinct_payments": len(ids),
                    "missing_legitimate_payments": len(missing),
                    "sample_missing": sorted(missing)[:5],
                    "unexpected_payments": len(extra),
                },
            )
        if "downstream_query" in want:
            rows = conn.execute(
                sql.SQL(
                    "SELECT date_trunc('week', business_date)::date AS wk, sum(net_revenue_paise) AS net "
                    "FROM {}.mart_daily_revenue GROUP BY 1 ORDER BY 1"
                ).format(s)
            ).fetchall()
            got = {r["wk"]: int(r["net"]) for r in rows}
            exp: dict[date, int] = defaultdict(int)
            for d, t in truth.daily.items():
                exp[_week(d)] += t.net_revenue_paise
            bad = sorted(str(w) for w in set(got) | set(exp) if got.get(w) != exp.get(w))
            add(
                "downstream_query",
                not bad,
                {"query": "weekly net revenue rollup", "mismatched_weeks": []},
                {"mismatched_weeks": bad, "weeks": len(got)},
            )
    return results


def _money(row: dict | None) -> tuple | None:
    if row is None:
        return None
    return (
        row["gross_collected_paise"],
        row["refunds_paise"],
        row["net_revenue_paise"],
        row["captured_payment_count"],
        row["refund_count"],
    )


def _week(d: date) -> date:
    from datetime import timedelta

    return d - timedelta(days=d.weekday())


def shadow_vs_canonical(shadow_schema: str, partition_scope: list[str]) -> dict:
    """Per-date net revenue of the shadow build vs canonical, for the repair review screen."""
    scope = set(partition_scope)
    with connect("validator") as conn:
        canonical = _mart(conn, "marts")
        shadow = _mart(conn, shadow_schema)
    rows = []
    for d in sorted(set(canonical) | set(shadow)):
        c, s_ = canonical.get(d), shadow.get(d)
        cn = c["net_revenue_paise"] if c else None
        sn = s_["net_revenue_paise"] if s_ else None
        if cn != sn or str(d) in scope:
            rows.append(
                {
                    "business_date": str(d),
                    "in_scope": str(d) in scope,
                    "canonical_net_paise": cn,
                    "shadow_net_paise": sn,
                    "delta_paise": (sn or 0) - (cn or 0),
                    "canonical_captured": c["captured_payment_count"] if c else None,
                    "shadow_captured": s_["captured_payment_count"] if s_ else None,
                }
            )
    return {
        "shadow_schema": shadow_schema,
        "shadow_available": True,
        "partitions_compared": len(set(canonical) | set(shadow)),
        "differences": rows,
        "captured_at": "validation",
    }
