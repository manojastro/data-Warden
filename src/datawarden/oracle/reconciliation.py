"""Independent revenue oracle computed from immutable source fixtures (not from dbt)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from datawarden import sources

IST = ZoneInfo("Asia/Kolkata")

# Ground-truth meaning of each source schema version's money field. This is the oracle's own
# knowledge of the producer semantics, independent of contracts and ingestion mappings.
_AMOUNT_FIELD = {"v1": "amount_paise", "v2": "total_amount_paise"}


@dataclass(frozen=True)
class DailyTruth:
    business_date: date
    gross_collected_paise: int
    refunds_paise: int
    captured_payment_count: int
    refund_count: int

    @property
    def net_revenue_paise(self) -> int:
        return self.gross_collected_paise - self.refunds_paise


@dataclass(frozen=True)
class SourceTruth:
    daily: dict[date, DailyTruth]
    distinct_payment_ids: int
    distinct_payment_event_ids: int
    captured_payment_ids: frozenset[str]
    manifest_digest: str


_cache: dict[str, SourceTruth] = {}


def _bdate(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).date()


def _latest_by(records: list[dict], key: str) -> dict[str, dict]:
    seen_events: set[str] = set()
    latest: dict[str, dict] = {}
    for rec in records:
        if rec["event_id"] in seen_events:
            continue  # re-delivered event: same fact
        seen_events.add(rec["event_id"])
        cur = latest.get(rec[key])
        if cur is None or (rec["event_ts"], rec["event_id"]) > (cur["event_ts"], cur["event_id"]):
            latest[rec[key]] = rec
    return latest


def source_truth() -> SourceTruth:
    entries = sources.entries()
    digest = hashlib.sha256("|".join(f"{e.batch_id}:{e.sha256}" for e in entries).encode()).hexdigest()
    if digest in _cache:
        return _cache[digest]
    payments: list[dict] = []
    refunds: list[dict] = []
    for e in entries:
        if e.entity == "payments":
            for r in sources.read_batch(e):
                r = dict(r)
                r["_amount"] = r[_AMOUNT_FIELD[e.schema_version]]
                payments.append(r)
        elif e.entity == "refunds":
            for r in sources.read_batch(e):
                r = dict(r)
                r["_amount"] = r["amount_paise"]
                refunds.append(r)
    pay_latest = _latest_by(payments, "payment_id")
    ref_latest = _latest_by(refunds, "refund_id")
    acc: dict[date, list[int]] = {}
    captured = set()
    for p in pay_latest.values():
        if p["status"] == "captured":
            row = acc.setdefault(_bdate(p["event_ts"]), [0, 0, 0, 0])
            row[0] += p["_amount"]
            row[2] += 1
            captured.add(p["payment_id"])
    for r in ref_latest.values():
        if r["status"] == "succeeded":
            row = acc.setdefault(_bdate(r["event_ts"]), [0, 0, 0, 0])
            row[1] += r["_amount"]
            row[3] += 1
    daily = {d: DailyTruth(d, v[0], v[1], v[2], v[3]) for d, v in sorted(acc.items())}
    truth = SourceTruth(daily, len(pay_latest), len({p["event_id"] for p in payments}), frozenset(captured), digest)
    _cache.clear()
    _cache[digest] = truth
    return truth


@dataclass(frozen=True)
class DateDiff:
    business_date: date
    field: str
    expected: int
    observed: int | None


def compare_daily(rows: list[dict], dates: set[date] | None = None) -> list[DateDiff]:
    """Compare mart rows (dicts with business_date and paise columns) against source truth."""
    truth = source_truth().daily
    by_date = {r["business_date"]: r for r in rows}
    diffs: list[DateDiff] = []
    for d in sorted(set(truth) | set(by_date)):
        if dates is not None and d not in dates:
            continue
        t = truth.get(d)
        r = by_date.get(d)
        for fld in (
            "gross_collected_paise",
            "refunds_paise",
            "net_revenue_paise",
            "captured_payment_count",
            "refund_count",
        ):
            expected = getattr(t, fld) if t else 0
            observed = r[fld] if r else None
            if observed != expected:
                diffs.append(DateDiff(d, fld, expected, observed))
    return diffs
