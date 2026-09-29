from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from datawarden.api.deps import SESSION_COOKIE, Principal, current_user, get_db
from datawarden.config import get_settings
from datawarden.services import auth
from datawarden.services.audit import audit

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    client = request.client.host if request.client else "unknown"
    if auth.rate_limited(f"{client}:{body.username}"):
        raise HTTPException(429, "too many login attempts; try again later")
    result = auth.login(db, body.username, body.password)
    if result is None:
        audit(
            db, f"anon:{client}", "auth.login_failed", "user", body.username[:64], request_id=request.state.request_id
        )
        raise HTTPException(401, "invalid username or password")
    token, sess, user = result
    audit(db, f"user:{user.username}", "auth.login", "user", user.id, request_id=request.state.request_id)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=get_settings().env != "local",
        max_age=auth.SESSION_HOURS * 3600,
        path="/",
    )
    return {"username": user.username, "role": user.role_id, "csrf_token": sess.csrf_token}


@router.post("/logout")
def logout(
    request: Request, response: Response, user: Principal = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    token = request.cookies.get(SESSION_COOKIE) or request.headers.get("authorization", "")[7:]
    auth.logout(db, token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: Principal = Depends(current_user)) -> dict:
    perms = sorted(p for p in auth.PERMISSIONS if auth.allowed(user.role, p))
    return {"username": user.username, "role": user.role, "csrf_token": user.csrf_token, "permissions": perms}
