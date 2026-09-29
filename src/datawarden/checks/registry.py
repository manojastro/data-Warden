"""Protected quality check definitions and their deterministic implementations.

Checks are application code, not agent-editable configuration. Repair patches may not touch
this package (enforced by proposal policy) and agents have no tool that writes check config.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import psycopg

from datawarden import sources
from datawarden.oracle import reconciliation

FRESHNESS_LAG_DAYS = {"customers": 2, "orders": 1, "payments": 1, "refunds": 2}
ANOMALY_WINDOW_DAYS = 14
ANOMALY_MAX_DEVIATION = 0.35
TZ = "Asia/Kolkata"


@dataclass(frozen=True)
class CheckDef:
    check_id: str
    check_type: str
    asset: str
    severity: str  # critical | high | warning
    description: str
    threshold: dict = field(default_factory=dict)


CHECKS: list[CheckDef] = [
    *[
        CheckDef(
            f"freshness.raw_{e[:-1] if e != 'customers' else 'customer'}_events",
            "freshness",
            f"raw_{e[:-1] if e != 'customers' else 'customer'}_events",
            "high",
            f"Newest ingested {e} business date is within {lag} day(s) of the newest delivery",
            {"max_lag_days": lag},
        )
        for e, lag in FRESHNESS_LAG_DAYS.items()
    ],
    CheckDef(
        "freshness.mart_daily_revenue",
        "freshness",
        "mart_daily_revenue",
        "high",
        "Mart contains the newest business date present in ingested captured payments",
        {"max_lag_days": 0},
    ),
    CheckDef(
        "unique.stg_payments.payment_event_id",
        "uniqueness",
        "stg_payments",
        "high",
        "One staging row per source payment event",
    ),
    CheckDef(
        "unique.fct_payments.payment_id", "uniqueness", "fct_payments", "critical", "One fact row per payment attempt"
    ),
    CheckDef("unique.fct_refunds.refund_id", "uniqueness", "fct_refunds", "critical", "One fact row per refund"),
    CheckDef("unique.fct_orders.order_id", "uniqueness", "fct_orders", "critical", "One fact row per order"),
    CheckDef(
        "unique.mart_daily_revenue.business_date",
        "uniqueness",
        "mart_daily_revenue",
        "critical",
        "One mart row per business date",
    ),
    CheckDef(
        "not_null.fct_payments",
        "required_fields",
        "fct_payments",
        "high",
        "payment_id, order_id, amount_paise, status, payment_business_date are present",
    ),
    CheckDef(
        "ref.fct_payments.order_id",
        "referential_integrity",
        "fct_payments",
        "warning",
        "Every payment references a known order (late orders tolerated)",
    ),
    CheckDef(
        "ref.fct_refunds.payment_id",
        "referential_integrity",
        "fct_refunds",
        "warning",
        "Every refund references a known payment",
    ),
    CheckDef(
        "state.fct_payments.status",
        "valid_states",
        "fct_payments",
        "high",
        "Payment status in {captured, failed, pending}",
    ),
    CheckDef("state.fct_refunds.status", "valid_states", "fct_refunds", "high", "Refund status in {succeeded, failed}"),
    *[
        CheckDef(
            f"contract.raw_{t}_events",
            "schema_contract",
            f"raw_{t}_events",
            "critical",
            f"Every delivered {e} batch conforms to a registered, mapped schema contract",
        )
        for e, t in (("customers", "customer"), ("orders", "order"), ("payments", "payment"), ("refunds", "refund"))
    ],
    CheckDef(
        "reconciliation.mart_daily_revenue",
        "reconciliation",
        "mart_daily_revenue",
        "critical",
        "Mart gross, refunds, net and counts equal an independent recomputation from source fixtures",
    ),
    CheckDef(
        "anomaly.captured_payments_volume",
        "row_count_anomaly",
        "fct_payments",
        "warning",
        "Latest-day captured payment count within ±35% of the trailing 14-day median",
        {"window_days": ANOMALY_WINDOW_DAYS, "max_deviation": ANOMALY_MAX_DEVIATION},
    ),
]
CHECKS_BY_ID = {c.check_id: c for c in CHECKS}


@dataclass
class CheckResult:
    check_id: str
    check_type: str
    asset: str
    status: str  # pass | fail | warn | error
    severity: str
    message: str
    observed: dict = field(default_factory=dict)
    threshold: dict = field(default_factory=dict)
    partition_date: date | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["partition_date"] = self.partition_date.isoformat() if self.partition_date else None
        return d


def _result(
    check_id: str, ok: bool, message: str, observed: dict | None = None, partition_date: date | None = None
) -> CheckResult:
    c = CHECKS_BY_ID[check_id]
    status = "pass" if ok else ("warn" if c.severity == "warning" else "fail")
    return CheckResult(
        c.check_id, c.check_type, c.asset, status, c.severity, message, observed or {}, c.threshold, partition_date
    )


def _one(conn: psycopg.Connection, query: str, params: tuple = ()) -> dict:
    return conn.execute(query, params).fetchone()


def _relation_exists(conn: psycopg.Connection, schema: str, name: str) -> bool:
    return _one(conn, "SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{name}",))["ok"]


def run_checks(
    conn: psycopg.Connection,
    *,
    schema_staging: str = "staging",
    schema_marts: str = "marts",
    include: set[str] | None = None,
) -> list[CheckResult]:
    """Run all (or selected) checks against canonical schemas. Read-only."""
    out: list[CheckResult] = []

    def want(cid: str) -> bool:
        return include is None or cid in include

    manifest = sources.entries()
    clock = max((date.fromisoformat(e.delivery_date) for e in manifest), default=None)

    # ---- freshness (synthetic clock = newest delivery date in the source manifest)
    for entity, table in (
        ("customers", "raw_customer_events"),
        ("orders", "raw_order_events"),
        ("payments", "raw_payment_events"),
        ("refunds", "raw_refund_events"),
    ):
        cid = f"freshness.{table}"
        if not want(cid) or clock is None:
            continue
        row = _one(conn, f"SELECT max((event_ts AT TIME ZONE '{TZ}')::date) AS d FROM raw.{table}")
        newest = row["d"]
        lag = (clock - newest).days if newest else None
        ok = lag is not None and lag <= FRESHNESS_LAG_DAYS[entity]
        out.append(
            _result(
                cid,
                ok,
                f"newest ingested business date {newest}, newest delivery {clock}, lag {lag} day(s)",
                {"newest_ingested_date": str(newest), "newest_delivery_date": str(clock), "lag_days": lag},
            )
        )

    marts_ok = all(
        _relation_exists(conn, schema_marts, t)
        for t in ("fct_payments", "fct_refunds", "fct_orders", "mart_daily_revenue")
    )
    if not marts_ok:
        for cid in CHECKS_BY_ID:
            if want(cid) and not cid.startswith(("freshness.raw", "contract.")):
                c = CHECKS_BY_ID[cid]
                out.append(
                    CheckResult(
                        cid,
                        c.check_type,
                        c.asset,
                        "error",
                        c.severity,
                        "canonical relation missing; check could not run",
                    )
                )
        out.extend(_contract_checks(conn, manifest, want))
        return out

    m, s = schema_marts, schema_staging
    if want("freshness.mart_daily_revenue"):
        row = _one(
            conn,
            f"""SELECT (SELECT max(business_date) FROM {m}.mart_daily_revenue) AS mart_max,
                              (SELECT max((event_ts AT TIME ZONE '{TZ}')::date) FROM raw.raw_payment_events
                               WHERE status = 'captured') AS raw_max""",
        )
        lag = (row["raw_max"] - row["mart_max"]).days if row["raw_max"] and row["mart_max"] else None
        out.append(
            _result(
                "freshness.mart_daily_revenue",
                lag is not None and lag <= 0,
                f"mart newest partition {row['mart_max']}, newest captured payment date {row['raw_max']}",
                {"mart_newest": str(row["mart_max"]), "raw_newest": str(row["raw_max"]), "lag_days": lag},
            )
        )

    uniq = [
        ("unique.stg_payments.payment_event_id", s, "stg_payments", "payment_event_id", "payment_business_date"),
        ("unique.fct_payments.payment_id", m, "fct_payments", "payment_id", "payment_business_date"),
        ("unique.fct_refunds.refund_id", m, "fct_refunds", "refund_id", "refund_business_date"),
        ("unique.fct_orders.order_id", m, "fct_orders", "order_id", "order_business_date"),
        ("unique.mart_daily_revenue.business_date", m, "mart_daily_revenue", "business_date", "business_date"),
    ]
    for cid, schema, table, key, datecol in uniq:
        if not want(cid):
            continue
        rows = conn.execute(f"""
            SELECT d, count(*) AS dup_keys, sum(n - 1) AS extra_rows FROM (
              SELECT {key} AS k, min({datecol}) AS d, count(*) AS n FROM {schema}.{table}
              GROUP BY {key} HAVING count(*) > 1) x GROUP BY 1 ORDER BY 1""").fetchall()
        extra = sum(r["extra_rows"] for r in rows)
        out.append(
            _result(
                cid,
                extra == 0,
                f"{extra} duplicate row(s) on {table}.{key}" if extra else f"{table}.{key} unique",
                {
                    "duplicate_rows": int(extra),
                    "duplicate_keys": int(sum(r["dup_keys"] for r in rows)),
                    "affected_dates": [str(r["d"]) for r in rows][:40],
                },
            )
        )

    if want("not_null.fct_payments"):
        row = _one(
            conn,
            f"""SELECT count(*) FILTER (WHERE payment_id IS NULL OR order_id IS NULL OR
                                amount_paise IS NULL OR status IS NULL OR payment_business_date IS NULL) AS n
                             FROM {m}.fct_payments""",
        )
        out.append(
            _result(
                "not_null.fct_payments",
                row["n"] == 0,
                f"{row['n']} row(s) with missing required fields",
                {"rows_with_nulls": row["n"]},
            )
        )
    if want("ref.fct_payments.order_id"):
        row = _one(
            conn,
            f"""SELECT count(*) AS n FROM {m}.fct_payments p
                             WHERE NOT EXISTS (SELECT 1 FROM {m}.fct_orders o WHERE o.order_id = p.order_id)""",
        )
        out.append(
            _result(
                "ref.fct_payments.order_id",
                row["n"] == 0,
                f"{row['n']} payment(s) without a known order",
                {"orphans": row["n"]},
            )
        )
    if want("ref.fct_refunds.payment_id"):
        row = _one(
            conn,
            f"""SELECT count(*) AS n FROM {m}.fct_refunds r
                             WHERE NOT EXISTS (SELECT 1 FROM {m}.fct_payments p WHERE p.payment_id = r.payment_id)""",
        )
        out.append(
            _result(
                "ref.fct_refunds.payment_id",
                row["n"] == 0,
                f"{row['n']} refund(s) without a known payment",
                {"orphans": row["n"]},
            )
        )
    if want("state.fct_payments.status"):
        row = _one(
            conn, f"SELECT count(*) AS n FROM {m}.fct_payments WHERE status NOT IN ('captured','failed','pending')"
        )
        out.append(
            _result(
                "state.fct_payments.status",
                row["n"] == 0,
                f"{row['n']} invalid payment state(s)",
                {"invalid": row["n"]},
            )
        )
    if want("state.fct_refunds.status"):
        row = _one(conn, f"SELECT count(*) AS n FROM {m}.fct_refunds WHERE status NOT IN ('succeeded','failed')")
        out.append(
            _result(
                "state.fct_refunds.status", row["n"] == 0, f"{row['n']} invalid refund state(s)", {"invalid": row["n"]}
            )
        )

    out.extend(_contract_checks(conn, manifest, want))

    if want("reconciliation.mart_daily_revenue"):
        mart_rows = conn.execute(f"""SELECT business_date, gross_collected_paise, refunds_paise, net_revenue_paise,
                                     captured_payment_count, refund_count FROM {m}.mart_daily_revenue""").fetchall()
        diffs = reconciliation.compare_daily(mart_rows)
        dates = sorted({d.business_date for d in diffs})
        net_diffs = {d.business_date: d for d in diffs if d.field == "net_revenue_paise"}
        detail = [
            {
                "business_date": str(d),
                "net_difference_paise": ((net_diffs[d].observed or 0) - net_diffs[d].expected) if d in net_diffs else 0,
                "fields": sorted({x.field for x in diffs if x.business_date == d}),
            }
            for d in dates
        ]
        msg = (
            f"{len(dates)} business date(s) disagree with independent source reconciliation"
            if dates
            else f"all {len(mart_rows)} mart partitions reconcile"
        )
        out.append(
            _result(
                "reconciliation.mart_daily_revenue",
                not dates,
                msg,
                {
                    "mismatched_dates": [str(d) for d in dates],
                    "detail": detail[:40],
                    "partitions_checked": len(mart_rows),
                },
            )
        )

    if want("anomaly.captured_payments_volume"):
        rows = conn.execute(f"""SELECT payment_business_date AS d, count(DISTINCT payment_id) AS n
                                FROM {m}.fct_payments WHERE status = 'captured'
                                GROUP BY 1 ORDER BY 1""").fetchall()
        if clock and len(rows) > ANOMALY_WINDOW_DAYS:
            by_day = {r["d"]: r["n"] for r in rows}
            latest = clock if clock in by_day else rows[-1]["d"]
            window = [
                by_day[d]
                for d in (latest - timedelta(days=i) for i in range(1, ANOMALY_WINDOW_DAYS + 1))
                if d in by_day
            ]
            median = statistics.median(window) if window else 0
            dev = (by_day.get(latest, 0) - median) / median if median else 0.0
            out.append(
                _result(
                    "anomaly.captured_payments_volume",
                    abs(dev) <= ANOMALY_MAX_DEVIATION,
                    f"{by_day.get(latest, 0)} captured payments on {latest} vs trailing median "
                    f"{median:.0f} ({dev:+.1%})",
                    {
                        "latest_date": str(latest),
                        "latest_count": by_day.get(latest, 0),
                        "baseline_median": median,
                        "deviation": round(dev, 4),
                    },
                    latest,
                )
            )
    return out


def _contract_checks(conn: psycopg.Connection, manifest: list, want) -> list[CheckResult]:
    out = []
    ingested = {r["batch_id"] for r in conn.execute("SELECT batch_id FROM ops.ingested_batches").fetchall()}
    for entity, table in (
        ("customers", "customer"),
        ("orders", "order"),
        ("payments", "payment"),
        ("refunds", "refund"),
    ):
        cid = f"contract.raw_{table}_events"
        if not want(cid):
            continue
        pending = [e.batch_id for e in manifest if e.entity == entity and e.batch_id not in ingested]
        rejected = []
        if pending:
            rows = conn.execute(
                """SELECT DISTINCT ON (batch_id) batch_id, schema_version, contract_status, detail
                                   FROM ops.schema_observations WHERE batch_id = ANY(%s)
                                   ORDER BY batch_id, observed_at DESC""",
                (pending,),
            ).fetchall()
            rejected = [dict(r) for r in rows if r["contract_status"] not in ("conforms", "mapped")]
        out.append(
            _result(
                cid,
                not rejected,
                f"{len(rejected)} delivered batch(es) rejected by schema contract"
                if rejected
                else f"all delivered {entity} batches conform",
                {"rejected_batches": rejected[:10], "pending_batches": len(pending)},
            )
        )
    return out


def summarize(results: list[CheckResult]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def store_results(conn: psycopg.Connection, results: list[CheckResult], run_id: str | None) -> None:
    with conn.transaction():
        cur = conn.cursor()
        for r in results:
            cur.execute(
                """INSERT INTO ops.quality_results (check_id, check_type, asset, run_id, status, severity,
                             partition_date, observed, threshold, message)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    r.check_id,
                    r.check_type,
                    r.asset,
                    run_id,
                    r.status,
                    r.severity,
                    r.partition_date,
                    json.dumps(r.observed, default=str),
                    json.dumps(r.threshold),
                    r.message,
                ),
            )
