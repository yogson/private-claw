"""
Security regression tests for the admin UI router.
Covers XSS escaping of untrusted path parameters in HTML responses.
"""

import pytest
from fastapi.testclient import TestClient

from assistant.admin.auth import _FALLBACK_SECRET, _SESSION_COOKIE, _serializer


def _set_session_cookie(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Injects a valid admin session cookie into the test client."""
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "test-token")
    token = _serializer().dumps({"authenticated": True})
    client.cookies.set(_SESSION_COOKIE, token)


# ---------------------------------------------------------------------------
# XSS: domain path parameter in /admin/config/{domain}/form
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "test-token")
    # Config files must exist for bootstrap; patch bootstrap to be a no-op
    # and pre-seed the runtime config via the dep.
    from unittest.mock import patch

    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        McpServersConfig,
        MemoryConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolsConfig,
    )

    fake_config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(
            default_model_id="claude-3-5-sonnet-20241022",
            model_allowlist=["claude-3-5-sonnet-20241022"],
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=[], denied_capabilities=[]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )

    with patch("assistant.core.bootstrap.bootstrap", return_value=fake_config):
        from assistant.api.deps import set_runtime_config
        from assistant.api.main import app

        set_runtime_config(fake_config)
        return TestClient(app, raise_server_exceptions=True)


def test_domain_form_xss_payload_is_escaped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain value containing HTML special chars must be escaped in the response.

    The payload is a single path segment (no embedded '/') so the route matches.
    HTTPX URL-encodes the special chars; FastAPI decodes them before passing to the
    handler, which must html_escape() before embedding in the HTML response.
    """
    import urllib.parse

    _set_session_cookie(client, monkeypatch)
    # Single-segment payload: no '/' so the route still matches {domain}.
    xss_domain = '"><img src=x onerror=alert(1)>'
    encoded = urllib.parse.quote(xss_domain, safe="")
    resp = client.get(f"/admin/config/{encoded}/form")
    assert resp.status_code == 200
    body = resp.text
    # Angle brackets must be escaped — there must be no live <img> element.
    assert "<img" not in body
    # Escaped form of the opening tag must be present.
    assert "&lt;img" in body


def test_domain_form_unknown_domain_returns_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown domain without allowlisted keys returns a warning, not an error."""
    _set_session_cookie(client, monkeypatch)
    resp = client.get("/admin/config/unknown_domain/form")
    assert resp.status_code == 200
    assert "no editable keys" in resp.text


# ---------------------------------------------------------------------------
# Fallback secret: cannot be predicted by using old literal
# ---------------------------------------------------------------------------


def test_fallback_secret_is_not_known_literal() -> None:
    """The per-process fallback secret must differ from the old hardcoded literal."""
    assert _FALLBACK_SECRET != "default-insecure-secret"


def test_fallback_secret_is_random_per_import() -> None:
    """The fallback secret is a non-empty hex string (not empty / trivial)."""
    assert len(_FALLBACK_SECRET) >= 32
    assert all(c in "0123456789abcdef" for c in _FALLBACK_SECRET)
