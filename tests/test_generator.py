import json
from collections import Counter
from datetime import date

from datawarden.generator.synthetic import generate_baseline


def _events(seed=42):
    return generate_baseline(seed, date(2026, 8, 1), 30)


def test_deterministic_and_large_enough():
    a, b = _events(), _events()
    assert json.dumps([x.records for x in a], sort_keys=True) == json.dumps([x.records for x in b], sort_keys=True)
    total = sum(len(x.records) for x in a)
    assert total >= 50_000
    assert len({x.delivery_date for x in a}) == 30


def test_different_seed_changes_data():
    assert json.dumps([x.records for x in _events(1)]) != json.dumps([x.records for x in _events(2)])


def test_money_is_integer_paise_and_ids_unique():
    batches = _events()
    event_ids = Counter()
    for b in batches:
        for r in b.records:
            event_ids[r["event_id"]] += 1
            for k, v in r.items():
                if k.endswith("_paise"):
                    assert isinstance(v, int) and v >= 0
    assert max(event_ids.values()) == 1


def test_contains_retries_partial_refunds_and_cancellations():
    batches = _events()
    payments = [r for b in batches if b.entity == "payments" for r in b.records]
    refunds = [r for b in batches if b.entity == "refunds" for r in b.records]
    orders = [r for b in batches if b.entity == "orders" for r in b.records]
    by_order = Counter(p["order_id"] for p in payments)
    # legitimate distinct payments on the same order with the same amount (retry after failure)
    assert sum(1 for n in by_order.values() if n > 1) > 500
    captured = {p["payment_id"]: p["amount_paise"] for p in payments if p["status"] == "captured"}
    assert any(r["amount_paise"] < captured.get(r["payment_id"], 0) for r in refunds)  # partial refunds
    assert any(o["event_type"] == "order_cancelled" for o in orders)
    assert {p["status"] for p in payments} == {"captured", "failed", "pending"}
