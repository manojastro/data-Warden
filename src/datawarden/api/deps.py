"""Shared API dependencies: DB session, authentication, permission checks, CSRF, pagination."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from datawarden.db.session import new_session
from datawarden.services import auth

SESSION_COOKIE = "dw_session"


def get_db() -> Iterator[Session]:
    db = new_session()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@dataclass
class Principal:
    user_id: str
    username: str
    role: str
    csrf_token: str
    via_cookie: bool

    @property
    def actor(self) -> str:
        return f"user:{self.username}"


def _token(request: Request) -> tuple[str | None, bool]:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip(), False
    return request.cookies.get(SESSION_COOKIE), True


def current_user(request: Request, db: Session = Depends(get_db)) -> Principal:
    token, via_cookie = _token(request)
    resolved = auth.resolve(db, token)
    if resolved is None:
        raise HTTPException(401, "authentication required")
    sess, user = resolved
    principal = Principal(user.id, user.username, user.role_id, sess.csrf_token, via_cookie)
    if via_cookie and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-csrf-token") != sess.csrf_token:
            raise HTTPException(403, "missing or invalid CSRF token")
    return principal


def require(permission: str):
    def dep(user: Principal = Depends(current_user)) -> Principal:
        if not auth.allowed(user.role, permission):
            raise HTTPException(403, f"role '{user.role}' lacks permission '{permission}'")
        return user

    return dep


def request_id(request: Request) -> str:
    return request.state.request_id


@dataclass
class Page:
    limit: int
    offset: int


def page(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000)) -> Page:
    return Page(limit, offset)


def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key")) -> str | None:
    if key is not None and not (8 <= len(key) <= 128):
        raise HTTPException(400, "Idempotency-Key must be 8-128 characters")
    return key
