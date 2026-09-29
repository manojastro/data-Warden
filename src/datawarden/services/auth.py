"""Password hashing, server-side sessions, and the role permission matrix."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from datawarden.config import get_settings
from datawarden.db.models import Session, User

_hasher = PasswordHasher()
SESSION_HOURS = 12

# Permission matrix (server-side). Roles do not inherit implicitly; each permission lists roles.
PERMISSIONS: dict[str, set[str]] = {
    "read": {"viewer", "operator", "approver"},
    "incident.start": {"operator", "approver"},
    "incident.cancel": {"operator", "approver"},
    "pipeline.run": {"operator", "approver"},
    "demo.control": {"operator", "approver"},
    "eval.run": {"operator", "approver"},
    "approval.decide": {"approver"},
}


def allowed(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_attempts: dict[str, deque] = defaultdict(deque)


def rate_limited(key: str, limit: int = 10, window: int = 300) -> bool:
    now = time.monotonic()
    q = _attempts[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def login(db: DbSession, username: str, password: str) -> tuple[str, Session, User] | None:
    user = db.scalar(select(User).where(User.username == username))
    # constant-ish work even for unknown users
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if user.disabled or not verify_password(user.password_hash, password):
        return None
    token = secrets.token_urlsafe(32)
    sess = Session(
        id=_token_id(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(24),
        expires_at=datetime.now(UTC) + timedelta(hours=SESSION_HOURS),
    )
    db.add(sess)
    return token, sess, user


def resolve(db: DbSession, token: str | None) -> tuple[Session, User] | None:
    if not token:
        return None
    sess = db.get(Session, _token_id(token))
    if sess is None or sess.revoked_at is not None or sess.expires_at < datetime.now(UTC):
        return None
    user = db.get(User, sess.user_id)
    if user is None or user.disabled:
        return None
    return sess, user


def logout(db: DbSession, token: str) -> None:
    sess = db.get(Session, _token_id(token))
    if sess and sess.revoked_at is None:
        sess.revoked_at = datetime.now(UTC)


_DUMMY_HASH = _hasher.hash("not-a-real-password")


def ensure_demo_users(db: DbSession) -> dict:
    """Create viewer/operator/approver demo accounts with locally generated passwords.

    Passwords are written once to ``artifacts/demo_credentials.json`` (mode 0600) and never logged.
    Existing accounts keep their passwords.
    """
    path = get_settings().artifact_dir / "demo_credentials.json"
    creds = json.loads(path.read_text()) if path.exists() else {}
    created = []
    for role in ("viewer", "operator", "approver"):
        if db.scalar(select(User).where(User.username == role)):
            continue
        password = creds.get(role) or secrets.token_urlsafe(12)
        creds[role] = password
        db.add(User(username=role, password_hash=hash_password(password), role_id=role))
        created.append(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, indent=1))
    path.chmod(0o600)
    return {"created": created, "credentials_file": str(path)}
