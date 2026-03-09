"""
Component ID: CMP_ADMIN_MINIMAL_UI

Cookie-based session authentication for the admin UI.
Signs a session payload with itsdangerous so no plaintext token is stored
on the client. The session is verified on every protected request.
"""

import os
import secrets

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

_SESSION_COOKIE = "admin_session"
_MAX_AGE = 3600 * 8  # 8 hours

# Ephemeral per-process fallback used only when the env var is unset.
# Because it is random and unknown to callers, it prevents forged cookies
# while still letting verify_admin_token block logins (no real secret = no
# session can be legitimately created).
_FALLBACK_SECRET: str = secrets.token_hex(32)


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("ASSISTANT_ADMIN_TOKEN") or _FALLBACK_SECRET
    return URLSafeTimedSerializer(secret, salt="admin-ui-session")


def create_session(response: Response) -> None:
    """Signs an authenticated payload and stores it as an HttpOnly cookie."""
    token = _serializer().dumps({"authenticated": True})
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_session(response: Response) -> None:
    """Removes the admin session cookie."""
    response.delete_cookie(_SESSION_COOKIE)


def is_authenticated(request: Request) -> bool:
    """Returns True if the request carries a valid, unexpired admin session.

    Checks both signature/age and that the payload explicitly contains
    ``{"authenticated": True}`` — a signed but arbitrary payload is rejected.
    """
    raw = request.cookies.get(_SESSION_COOKIE)
    if not raw:
        return False
    try:
        payload = _serializer().loads(raw, max_age=_MAX_AGE)
        return payload.get("authenticated") is True
    except BadSignature:
        return False


def verify_admin_token(submitted: str) -> bool:
    """Validates a submitted login token against ASSISTANT_ADMIN_TOKEN env var."""
    expected = os.environ.get("ASSISTANT_ADMIN_TOKEN", "")
    if not expected:
        return False
    return submitted == expected
