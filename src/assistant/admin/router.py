"""
Component ID: CMP_ADMIN_MINIMAL_UI

Admin UI FastAPI router: server-rendered HTML with HTMX for config management.
Provides login/logout, config dashboard, and the validate → diff → apply workflow.
All mutation is delegated to the existing API layer; this router only renders UI.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import escape as html_escape

from assistant.admin.auth import clear_session, create_session, is_authenticated, verify_admin_token
from assistant.api.deps import get_runtime_config
from assistant.api.routers.config import (
    _ALLOWLISTED_KEYS,
    ApplyRequest,
    ValidateRequest,
    apply_config,
    diff_config,
    validate_config,
)

router = APIRouter(prefix="/admin", tags=["admin-ui"])

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_templates.env.filters["pluralize"] = lambda n, singular="", plural="s": (
    singular if int(n) == 1 else plural
)

_DOMAIN_LABELS: dict[str, str] = {
    "app": "Application Runtime",
    "telegram": "Telegram Channel",
    "model": "Model Routing",
    "capabilities": "Capabilities & Skills",
    "mcp_servers": "MCP Servers",
    "scheduler": "Scheduler",
    "store": "Store / Persistence",
}

_DOMAIN_ORDER = ["app", "telegram", "model", "capabilities", "mcp_servers", "scheduler", "store"]

# ---------------------------------------------------------------------------
# Runtime reconciliation advisory table
#
# Mirrors the Runtime Applicability Matrix in docs/assistant_v1/domains/admin.md.
# Three levels:
#   "none"        – confirmed live reload; subsystem wired for dynamic consumption.
#   "recommended" – RuntimeConfig object is updated but in-flight behavioral effect
#                   is unconfirmed because the consuming subsystem is not yet wired
#                   for dynamic reload.
#   "required"    – subsystem reads config only at startup; restart is mandatory.
#
# Advance this to "none" only when the consuming subsystem explicitly handles
# the RuntimeConfig reload signal and the behavior is integration-tested.
# ---------------------------------------------------------------------------
_DOMAIN_RESTART_ADVISORY: dict[str, str] = {
    "app": "recommended",  # log_level / timezone — no consumer wired yet
    "model": "recommended",  # model routing — no consumer wired yet
    "capabilities": "recommended",  # capability policy — no consumer wired yet
    "scheduler": "recommended",  # tick / lateness / jobs — no consumer wired yet
    "store": "recommended",  # lock TTL / retention — no consumer wired yet
}

_RESTART_ADVISORY_LABELS: dict[str, str] = {
    "none": "Domain reloaded into live runtime. No restart required.",
    "recommended": (
        "Changes persisted and RuntimeConfig updated. "
        "Restart recommended — in-flight behavioral effect is not yet confirmed "
        "for this domain (subsystem not wired for dynamic reload)."
    ),
    "required": "Restart required for changes to take effect.",
}

# Field type definitions for rendering the edit form.
_FIELD_DEFS: dict[str, dict[str, Any]] = {
    "log_level": {"type": "select", "options": ["DEBUG", "INFO", "WARNING", "ERROR"]},
    "timezone": {"type": "text"},
    "default_model_id": {"type": "text"},
    "quality_routing": {"type": "select", "options": ["quality_first", "cost_first"]},
    "max_tokens_default": {"type": "number"},
    "allowed_capabilities": {"type": "textarea", "encoding": "line_list"},
    "denied_capabilities": {"type": "textarea", "encoding": "line_list"},
    "command_allowlist": {"type": "textarea", "encoding": "line_list"},
    "tick_seconds": {"type": "number"},
    "max_lateness_seconds": {"type": "number"},
    "max_jobs": {"type": "number"},
    "lock_ttl_seconds": {"type": "number"},
    "idempotency_retention_seconds": {"type": "number"},
}

_SESSION_EXPIRED_HTML = (
    '<div class="alert alert-danger m-2">Session expired. '
    'Please <a href="/admin/login">log in</a> again.</div>'
)


def _redirect(path: str, status_code: int = 302) -> RedirectResponse:
    return RedirectResponse(path, status_code=status_code)


def _build_fields(allowed_keys: set[str], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Builds a list of field descriptor dicts for rendering the edit form."""
    fields = []
    for key in sorted(allowed_keys):
        value = current.get(key, "")
        defn = _FIELD_DEFS.get(key, {"type": "text"})
        display_value: Any = value
        if defn.get("encoding") == "line_list" and isinstance(value, list):
            display_value = "\n".join(str(v) for v in value)
        fields.append(
            {
                "name": key,
                "label": key.replace("_", " ").title(),
                "type": defn.get("type", "text"),
                "options": defn.get("options", []),
                "encoding": defn.get("encoding"),
                "value": display_value,
            }
        )
    return fields


def _parse_form_payload(domain: str, form: Any) -> dict[str, Any]:
    """Coerces raw HTML form strings into typed Python values for the given domain."""
    allowed_keys = _ALLOWLISTED_KEYS.get(domain, set())
    payload: dict[str, Any] = {}
    for key in allowed_keys:
        raw = form.get(key)
        if raw is None:
            continue
        defn = _FIELD_DEFS.get(key, {"type": "text"})
        if defn.get("type") == "number":
            try:
                payload[key] = int(raw)
            except (ValueError, TypeError):
                payload[key] = raw
        elif defn.get("encoding") == "line_list":
            payload[key] = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
        else:
            payload[key] = raw
    return payload


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    """Renders the admin login form."""
    if is_authenticated(request):
        return _redirect("/admin/config")
    return _templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, token: str = Form(...)) -> Response:
    """Validates the admin token; on success sets a session cookie and redirects."""
    if verify_admin_token(token):
        response = _redirect("/admin/config")
        create_session(response)
        return response
    return _templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid token. Please try again."},
        status_code=401,
    )


@router.get("/logout")
async def logout(request: Request) -> Response:
    """Clears the admin session cookie and redirects to the login page."""
    response = _redirect("/admin/login")
    clear_session(response)
    return response


# ---------------------------------------------------------------------------
# Config dashboard
# ---------------------------------------------------------------------------


@router.get("/config", response_class=HTMLResponse)
async def config_dashboard(request: Request) -> Response:
    """Renders the main configuration dashboard showing all domains."""
    if not is_authenticated(request):
        return _redirect("/admin/login")

    from assistant.api.routers.config import get_effective_config

    effective = await get_effective_config()

    domains = []
    for domain in _DOMAIN_ORDER:
        domain_cfg = effective.config.get(domain, {})
        prov = {
            k.split(".", 1)[1]: v
            for k, v in effective.provenance.items()
            if k.startswith(f"{domain}.")
        }
        domains.append(
            {
                "id": domain,
                "label": _DOMAIN_LABELS.get(domain, domain),
                "config": domain_cfg,
                "provenance": prov,
                "has_editable": domain in _ALLOWLISTED_KEYS,
            }
        )

    return _templates.TemplateResponse(request, "config.html", {"domains": domains})


# ---------------------------------------------------------------------------
# HTMX partial endpoints
# ---------------------------------------------------------------------------


@router.get("/config/{domain}/form", response_class=HTMLResponse)
async def domain_form(request: Request, domain: str) -> Response:
    """Returns the HTMX partial containing the editable form for a domain."""
    if not is_authenticated(request):
        return HTMLResponse(_SESSION_EXPIRED_HTML)

    allowed_keys = _ALLOWLISTED_KEYS.get(domain, set())
    if not allowed_keys:
        return HTMLResponse(
            f'<div class="alert alert-warning m-2">Domain <code>{html_escape(domain)}</code> '
            "has no editable keys in v1.</div>"
        )

    from assistant.api.routers.config import get_effective_config

    effective = await get_effective_config()
    domain_cfg = effective.config.get(domain, {})
    fields = _build_fields(allowed_keys, domain_cfg)

    return _templates.TemplateResponse(
        request,
        "partials/domain_form.html",
        {"domain": domain, "label": _DOMAIN_LABELS.get(domain, domain), "fields": fields},
    )


@router.post("/config/{domain}/diff", response_class=HTMLResponse)
async def domain_diff(request: Request, domain: str) -> Response:
    """Validates the form payload and returns the HTMX diff preview partial."""
    if not is_authenticated(request):
        return HTMLResponse(_SESSION_EXPIRED_HTML)

    form = await request.form()
    payload = _parse_form_payload(domain, form)

    validate_resp = await validate_config(ValidateRequest(domain=domain, payload=payload))
    if not validate_resp.valid:
        errors_html = "".join(f"<li>{e}</li>" for e in validate_resp.errors)
        return HTMLResponse(
            f'<div class="alert alert-danger mt-2"><strong>Validation errors:</strong>'
            f"<ul class='mb-0 mt-1'>{errors_html}</ul></div>"
        )

    runtime_config = get_runtime_config()
    diff_resp = await diff_config(runtime_config, ValidateRequest(domain=domain, payload=payload))

    advisory = _DOMAIN_RESTART_ADVISORY.get(domain, "recommended")
    return _templates.TemplateResponse(
        request,
        "partials/diff_result.html",
        {
            "domain": domain,
            "changes": [c.model_dump() for c in diff_resp.changes],
            "blocked_keys": diff_resp.blocked_keys,
            "payload_json": json.dumps(payload),
            "restart_advisory": advisory,
            "restart_advisory_label": _RESTART_ADVISORY_LABELS[advisory],
        },
    )


@router.post("/config/{domain}/apply", response_class=HTMLResponse)
async def domain_apply(request: Request, domain: str) -> Response:
    """Applies the config payload and returns the HTMX apply result partial."""
    if not is_authenticated(request):
        return HTMLResponse(_SESSION_EXPIRED_HTML)

    form = await request.form()
    payload = _parse_form_payload(domain, form)

    runtime_config = get_runtime_config()
    apply_resp = await apply_config(
        runtime_config,
        ApplyRequest(domain=domain, payload=payload, confirm=True),
    )

    advisory = _DOMAIN_RESTART_ADVISORY.get(domain, "recommended")
    return _templates.TemplateResponse(
        request,
        "partials/apply_result.html",
        {
            "domain": domain,
            "applied": apply_resp.applied,
            "message": apply_resp.message,
            "live_reloaded": apply_resp.live_reloaded,
            "restart_advisory": advisory,
            "restart_advisory_label": _RESTART_ADVISORY_LABELS[advisory],
        },
    )
