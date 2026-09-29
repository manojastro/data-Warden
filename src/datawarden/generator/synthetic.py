"""Deterministic synthetic retail event generator.

Produces source *batches* (one per entity per delivery date) for a retail revenue pipeline:
customers, orders, payments (with retries), and refunds (full and partial). All money is integer
paise. All randomness comes from a single seeded ``random.Random`` so a seed fully reproduces
every event, identifier, and batch checksum.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

GENERATOR_VERSION = "1.0.0"
IST = ZoneInfo("Asia/Kolkata")
ENTITIES = ("customers", "orders", "payments", "refunds")

_FIRST = [
    "Aarav",
    "Diya",
    "Kabir",
    "Ananya",
    "Vivaan",
    "Isha",
    "Arjun",
    "Meera",
    "Rohan",
    "Saanvi",
    "Aditya",
    "Kavya",
    "Nikhil",
    "Priya",
    "Rahul",
    "Sneha",
    "Varun",
    "Tara",
    "Yash",
    "Zoya",
]
_LAST = [
    "Sharma",
    "Iyer",
    "Reddy",
    "Nair",
    "Patel",
    "Gupta",
    "Menon",
    "Rao",
    "Singh",
    "Das",
    "Kulkarni",
    "Joshi",
    "Bose",
    "Khan",
    "Pillai",
    "Mehta",
    "Chopra",
    "Varma",
    "Shah",
    "Ghosh",
]
_CITIES = ["Bengaluru", "Mumbai", "Delhi", "Chennai", "Hyderabad", "Pune", "Kolkata", "Kochi", "Jaipur"]
_NOTES = [
    "",
    "",
    "",
    "",
    "",
    "Leave at the door",
    "Call on arrival",
    "Gift wrap please",
    "Deliver after 6pm",
    "Fragile items",
    "Ring the bell twice",
]
_METHODS = ["upi", "upi", "upi", "card", "card", "netbanking", "wallet"]


@dataclass
class Batch:
    entity: str
    delivery_date: date
    schema_version: str
    records: list[dict] = field(default_factory=list)

    @property
    def batch_id(self) -> str:
        prefix = {"customers": "cus", "orders": "ord", "payments": "pay", "refunds": "rfd"}[self.entity]
        return f"{prefix}-{self.delivery_date:%Y%m%d}-{self.schema_version}"


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def business_date(ts: datetime) -> date:
    return ts.astimezone(IST).date()


class SyntheticWorld:
    """Stateful generator. Days are generated strictly in order; state carries across days."""

    def __init__(self, seed: int, start: date, baseline_days: int, base_orders_per_day: int = 760):
        self.seed = seed
        self.rng = random.Random(seed)
        self.start = start
        self.baseline_days = baseline_days
        self.base_orders = base_orders_per_day
        self.customers: list[str] = []
        self.counters = {"customer": 0, "order": 0, "payment": 0, "refund": 0}
        # future-dated events waiting for their business date: date -> entity -> [(ts, record)]
        self.pending: dict[date, dict[str, list[tuple[datetime, dict]]]] = {}
        # events whose delivery slipped to the next day's batch
        self.late_carry: dict[date, dict[str, list[tuple[datetime, dict]]]] = {}
        self.day_index = 0

    # -- identifiers -------------------------------------------------------------------------
    def _event_id(self) -> str:
        return f"evt_{self.rng.getrandbits(64):016x}"

    def _next(self, kind: str, prefix: str) -> str:
        self.counters[kind] += 1
        return f"{prefix}_{self.counters[kind]:07d}"

    def _schedule(self, ts: datetime, entity: str, record: dict) -> None:
        d = business_date(ts)
        self.pending.setdefault(d, {}).setdefault(entity, []).append((ts, record))

    def _random_ts(self, d: date) -> datetime:
        # Diurnal pattern: most orders between 08:00 and 23:59 IST.
        if self.rng.random() < 0.9:
            seconds = self.rng.randint(8 * 3600, 24 * 3600 - 1)
        else:
            seconds = self.rng.randint(0, 8 * 3600 - 1)
        return datetime.combine(d, time(0), IST) + timedelta(seconds=seconds)

    # -- entities ----------------------------------------------------------------------------
    def _customer_event(self, ts: datetime, customer_id: str, event_type: str) -> dict:
        first, last = self.rng.choice(_FIRST), self.rng.choice(_LAST)
        return {
            "event_id": self._event_id(),
            "event_type": event_type,
            "customer_id": customer_id,
            "full_name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}{self.rng.randint(1, 999)}@example.test",
            "phone": f"+91-9{self.rng.randint(100000000, 999999999)}",
            "city": self.rng.choice(_CITIES),
            "event_ts": _iso(ts),
            "schema_version": "v1",
        }

    def generate_day(self, volume_factor: float = 1.0, schema_versions: dict[str, str] | None = None) -> list[Batch]:
        """Generate the next business day and return its batches (one per entity)."""
        rng = self.rng
        d = self.start + timedelta(days=self.day_index)
        self.day_index += 1
        is_last_baseline_day = self.day_index >= self.baseline_days
        day_start = datetime.combine(d, time(0), IST)

        if self.day_index == 1:
            for _ in range(3000):
                cid = self._next("customer", "cus")
                self.customers.append(cid)
                ts = day_start + timedelta(seconds=rng.randint(0, 3600))
                self._schedule(ts, "customers", self._customer_event(ts, cid, "customer_created"))
        for _ in range(rng.randint(25, 40)):
            cid = self._next("customer", "cus")
            self.customers.append(cid)
            ts = self._random_ts(d)
            self._schedule(ts, "customers", self._customer_event(ts, cid, "customer_created"))
        for _ in range(rng.randint(10, 25)):
            ts = self._random_ts(d)
            self._schedule(ts, "customers", self._customer_event(ts, rng.choice(self.customers), "customer_updated"))

        weekday_boost = 1.15 if d.weekday() >= 5 else 1.0
        n_orders = int(round(self.base_orders * weekday_boost * volume_factor * rng.uniform(0.93, 1.07)))
        for _ in range(n_orders):
            self._generate_order(d)

        # Collect every event whose business date is today; decide delivery date.
        todays = self.pending.pop(d, {})
        batches = {e: Batch(e, d, (schema_versions or {}).get(e, "v1")) for e in ENTITIES}
        late: dict[str, list[tuple[datetime, dict]]] = {}
        for entity in ENTITIES:
            for ts, rec in sorted(todays.get(entity, []), key=lambda x: (x[0], x[1]["event_id"])):
                # A small share of order/payment events arrives in the next day's delivery. This
                # is normal lateness, inside the mart lookback window.
                goes_late = entity in ("orders", "payments") and rng.random() < 0.015
                if goes_late and not is_last_baseline_day:
                    late.setdefault(entity, []).append((ts, rec))
                else:
                    batches[entity].records.append(rec)
        # carry late events to tomorrow's delivery
        tomorrow = d + timedelta(days=1)
        for entity, items in late.items():
            self.late_carry.setdefault(tomorrow, {}).setdefault(entity, []).extend(items)
        for entity, items in self.late_carry.pop(d, {}).items():
            batches[entity].records.extend(rec for _, rec in items)
        return [b for b in batches.values() if b.records]

    def _generate_order(self, d: date) -> None:
        rng = self.rng
        oid = self._next("order", "ord")
        cid = rng.choice(self.customers)
        created = self._random_ts(d)
        total = rng.randint(199, 4999) * 100 + rng.choice([0, 0, 0, 50, 99])
        self._schedule(
            created,
            "orders",
            {
                "event_id": self._event_id(),
                "event_type": "order_created",
                "order_id": oid,
                "customer_id": cid,
                "order_total_paise": total,
                "currency": "INR",
                "customer_note": rng.choice(_NOTES),
                "event_ts": _iso(created),
                "schema_version": "v1",
            },
        )
        cancelled = rng.random() < 0.05
        if cancelled:
            cts = created + timedelta(minutes=rng.randint(5, 48 * 60))
            self._schedule(
                cts,
                "orders",
                {
                    "event_id": self._event_id(),
                    "event_type": "order_cancelled",
                    "order_id": oid,
                    "customer_id": cid,
                    "order_total_paise": total,
                    "currency": "INR",
                    "customer_note": "",
                    "event_ts": _iso(cts),
                    "schema_version": "v1",
                },
            )
            if rng.random() < 0.5:
                self._payment(oid, total, created, 1, "failed")
            return
        r = rng.random()
        if r < 0.85:
            outcomes = ["captured"]
        elif r < 0.95:
            outcomes = ["failed", "captured"]
        elif r < 0.98:
            outcomes = ["pending"]
        else:
            outcomes = ["failed"]
        ts = created
        for attempt, status in enumerate(outcomes, start=1):
            ts = ts + timedelta(minutes=rng.randint(1, 40))
            pid = self._payment(oid, total, ts, attempt, status)
            if status == "captured" and rng.random() < 0.07:
                self._refunds(oid, pid, total, ts)

    def _payment(self, oid: str, amount: int, ts: datetime, attempt: int, status: str) -> str:
        pid = self._next("payment", "pay")
        self._schedule(
            ts,
            "payments",
            {
                "event_id": self._event_id(),
                "payment_id": pid,
                "order_id": oid,
                "attempt_no": attempt,
                "status": status,
                "amount_paise": amount,
                "currency": "INR",
                "method": self.rng.choice(_METHODS),
                "event_ts": _iso(ts),
                "schema_version": "v1",
            },
        )
        return pid

    def _refunds(self, oid: str, pid: str, captured: int, paid_ts: datetime) -> None:
        rng = self.rng
        if rng.random() < 0.6:
            parts = [captured]
        else:
            first = captured * rng.randint(20, 60) // 100
            parts = [first] if rng.random() < 0.7 else [first, (captured - first) * rng.randint(20, 90) // 100]
        ts = paid_ts
        for amount in parts:
            ts = ts + timedelta(hours=rng.randint(12, 96))
            status = "failed" if rng.random() < 0.05 else "succeeded"
            self._schedule(
                ts,
                "refunds",
                {
                    "event_id": self._event_id(),
                    "refund_id": self._next("refund", "rfd"),
                    "payment_id": pid,
                    "order_id": oid,
                    "amount_paise": amount,
                    "currency": "INR",
                    "status": status,
                    "event_ts": _iso(ts),
                    "schema_version": "v1",
                },
            )


def generate_baseline(seed: int, start: date, days: int) -> list[Batch]:
    world = SyntheticWorld(seed, start, days)
    batches: list[Batch] = []
    for _ in range(days):
        batches.extend(world.generate_day())
    return batches


def generate_extra_day(
    seed: int, start: date, baseline_days: int, volume_factor: float, schema_versions: dict[str, str] | None = None
) -> list[Batch]:
    """Replay the baseline deterministically, then generate the next day.

    Used by fault scenarios that need an additional delivery day (legitimate decline, schema
    drift). The baseline days are regenerated only to advance the random stream and state.
    """
    world = SyntheticWorld(seed, start, baseline_days)
    for _ in range(baseline_days):
        world.generate_day()
    return world.generate_day(volume_factor=volume_factor, schema_versions=schema_versions)
