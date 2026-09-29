"""HMAC signing for service-to-service event callbacks (pipeline runner, Airflow)."""

from __future__ import annotations

import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 300


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def headers(secret: str, body: bytes, idempotency_key: str) -> dict[str, str]:
    ts = str(int(time.time()))
    return {
        "X-DW-Timestamp": ts,
        "X-DW-Signature": sign(secret, ts, body),
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
    }


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        skew = abs(time.time() - int(timestamp))
    except ValueError:
        return False
    if skew > MAX_SKEW_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)
