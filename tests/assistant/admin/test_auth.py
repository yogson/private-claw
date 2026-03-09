"""
Tests for admin session authentication helpers.
"""

import pytest
from fastapi import Request
from itsdangerous import URLSafeTimedSerializer
from starlette.datastructures import Headers
from starlette.types import Scope

from assistant.admin.auth import (
    _SESSION_COOKIE,
    _serializer,
    is_authenticated,
    verify_admin_token,
)

# ---------------------------------------------------------------------------
# verify_admin_token
# ---------------------------------------------------------------------------


def test_verify_admin_token_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "supersecret")
    assert verify_admin_token("supersecret") is True


def test_verify_admin_token_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "supersecret")
    assert verify_admin_token("wrongtoken") is False


def test_verify_admin_token_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASSISTANT_ADMIN_TOKEN", raising=False)
    assert verify_admin_token("anything") is False


# ---------------------------------------------------------------------------
# is_authenticated
# ---------------------------------------------------------------------------


def _make_request(cookies: dict[str, str]) -> Request:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": Headers({"cookie": cookie_header}).raw if cookie_header else [],
    }
    return Request(scope)


def test_is_authenticated_no_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "secret")
    req = _make_request({})
    assert is_authenticated(req) is False


def test_is_authenticated_valid_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "secret")
    token = _serializer().dumps({"authenticated": True})
    req = _make_request({_SESSION_COOKIE: token})
    assert is_authenticated(req) is True


def test_is_authenticated_tampered_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "secret")
    req = _make_request({_SESSION_COOKIE: "tampered.data.here"})
    assert is_authenticated(req) is False


def test_is_authenticated_signed_with_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "secret-A")
    bad_serializer = URLSafeTimedSerializer("secret-B", salt="admin-ui-session")
    token = bad_serializer.dumps({"authenticated": True})
    req = _make_request({_SESSION_COOKIE: token})
    assert is_authenticated(req) is False


# ---------------------------------------------------------------------------
# Security regression: fallback secret is not guessable
# ---------------------------------------------------------------------------


def test_is_authenticated_rejects_cookie_signed_with_known_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forging a cookie using the old hardcoded fallback must be rejected."""
    monkeypatch.delenv("ASSISTANT_ADMIN_TOKEN", raising=False)
    known_fallback = URLSafeTimedSerializer("default-insecure-secret", salt="admin-ui-session")
    forged_token = known_fallback.dumps({"authenticated": True})
    req = _make_request({_SESSION_COOKIE: forged_token})
    # The per-process random _FALLBACK_SECRET makes the old literal unusable.
    assert is_authenticated(req) is False


def test_is_authenticated_rejects_arbitrary_signed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correctly signed token whose payload lacks the auth claim is rejected."""
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "secret")
    # Sign a payload that is valid but does not contain authenticated=True.
    token = _serializer().dumps({"role": "admin"})
    req = _make_request({_SESSION_COOKIE: token})
    assert is_authenticated(req) is False


def test_is_authenticated_rejects_false_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """authenticated=False in a correctly signed payload must be rejected."""
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "secret")
    token = _serializer().dumps({"authenticated": False})
    req = _make_request({_SESSION_COOKIE: token})
    assert is_authenticated(req) is False
