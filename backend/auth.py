"""Authentication: bcrypt verification, server-side sessions, cookie handling.

The session token lives in an HttpOnly cookie and NEVER in localStorage, so a
cross-site script cannot read it. Authorisation is decided here, on the server,
from that cookie -- the client's copy of `user` is display state only and is
never trusted for access decisions.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
from fastapi import Cookie, HTTPException, Response, status

from .db import query

SESSION_COOKIE = "laynes_session"
SESSION_TTL_DAYS = 30

# Secure must be off for plain-http local previews or the browser silently
# drops the cookie and every request looks logged out. It defaults to ON so a
# deployment is secure unless someone deliberately opts out.
COOKIE_SECURE = os.getenv("API_COOKIE_SECURE", "1") not in ("0", "false", "False")


def verify_login(email: str, password: str):
    user = query(
        "SELECT id, email, full_name, role, is_active, password_hash "
        "FROM app_users WHERE email = %s",
        (email.strip().lower(),), fetch="one",
    )
    # Compare against a dummy hash when the user is unknown so a missing
    # account and a wrong password take the same time to answer.
    stored = user["password_hash"] if user else (
        "$2b$12$" + "x" * 53)
    ok = False
    try:
        ok = bcrypt.checkpw(password.encode(), stored.encode())
    except ValueError:
        ok = False
    if not user or not user["is_active"] or not ok:
        return None
    query("UPDATE app_users SET last_login_at = NOW() WHERE id = %s",
          (user["id"],), fetch=None, commit=True)
    user.pop("password_hash", None)
    return user


def create_session(user_id) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    query("INSERT INTO app_sessions (token, user_id, expires_at) VALUES (%s,%s,%s)",
          (token, user_id, expires), fetch=None, commit=True)
    return token


def get_session_user(token: str | None):
    if not token:
        return None
    row = query(
        "SELECT u.id, u.email, u.full_name, u.role, u.is_active "
        "FROM app_sessions s JOIN app_users u ON u.id = s.user_id "
        "WHERE s.token = %s AND s.expires_at > NOW()",
        (token,), fetch="one",
    )
    if not row or not row["is_active"]:
        return None
    return row


def delete_session(token: str | None):
    if not token:
        return
    try:
        query("DELETE FROM app_sessions WHERE token = %s", (token,),
              fetch=None, commit=True)
    except psycopg2.Error:
        pass


def set_cookie(response: Response, token: str):
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,           # unreadable from JavaScript
        secure=COOKIE_SECURE,
        samesite="lax",          # blocks cross-site POSTs carrying the cookie
        path="/",
    )


def clear_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(laynes_session: str | None = Cookie(default=None)):
    """FastAPI dependency: the signed-in user, or 401.

    Every protected route depends on this, so authorisation is never a
    client-side decision.
    """
    user = get_session_user(laynes_session)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not signed in")
    return user


def require_admin(user=None):
    if not user or user["role"] != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Admin only")
    return user
