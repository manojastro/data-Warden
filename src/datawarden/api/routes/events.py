"""Signed, idempotent incident-event ingestion (pipeline runner and Airflow callbacks)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from datawarden.api.deps import get_db
from datawarden.config import get_settings
from datawarden.contracts.events import IncidentEventIn
from datawarden.events import signing
from datawarden.services.incidents import ingest_event

router = APIRouter(tags=["events"])


@router.post("/events", status_code=202)
async def post_event(
    request: Request,
    db: Session = Depends(get_db),
    x_dw_timestamp: str | None = Header(default=None),
    x_dw_signature: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    body = await request.body()
    secret = get_settings().ingest_hmac_secret.get_secret_value()
    if not signing.verify(secret, x_dw_timestamp, x_dw_signature, body):
        raise HTTPException(401, "invalid or missing event signature")
    if not idempotency_key or not (8 <= len(idempotency_key) <= 200):
        raise HTTPException(400, "Idempotency-Key header required")
    try:
        event = IncidentEventIn.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, f"invalid event: {exc.errors()[:3]}") from exc
    return ingest_event(
        db, event, idempotency_key, actor=f"service:{event.source}", request_id=request.state.request_id
    )
