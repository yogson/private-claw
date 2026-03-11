"""
Component ID: CMP_API_FASTAPI_GATEWAY

Admin config endpoints: read effective config, validate candidate config,
preview diff, and apply config updates with live runtime state reload.
"""

from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ValidationError

from assistant.api.deps import AdminAuthDep, RuntimeConfigDep, update_runtime_config_domain
from assistant.core.config.loader import ConfigLoader, resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig

router = APIRouter(prefix="/admin/config", tags=["admin-config"])

_ALLOWLISTED_KEYS: dict[str, set[str]] = {
    "app": {"log_level", "timezone"},
    "telegram": {
        "bot_token",
        "allowlist",
        "poll_timeout_seconds",
        "poll_interval_seconds",
        "startup_drop_pending_updates",
    },
    "model": {"default_model_id", "model_allowlist", "quality_routing", "max_tokens_default"},
    "capabilities": {"allowed_capabilities", "denied_capabilities", "command_allowlist"},
    "scheduler": {"tick_seconds", "max_lateness_seconds", "max_jobs"},
    "store": {"lock_ttl_seconds", "idempotency_retention_seconds"},
}

_FILENAME_MAP: dict[str, str] = {
    "app": "app.yaml",
    "telegram": "channel.telegram.yaml",
    "model": "model.yaml",
    "capabilities": "capabilities.yaml",
    "mcp_servers": "mcp_servers.yaml",
    "scheduler": "scheduler.yaml",
    "store": "store.yaml",
}


class EffectiveConfigResponse(BaseModel):
    config: dict[str, Any]
    provenance: dict[str, str]


class ValidateRequest(BaseModel):
    domain: str
    payload: dict[str, Any]


class ValidateResponse(BaseModel):
    valid: bool
    errors: list[str]


class DiffEntry(BaseModel):
    key: str
    current: Any
    proposed: Any


class DiffResponse(BaseModel):
    changes: list[DiffEntry]
    blocked_keys: list[str]


class ApplyRequest(BaseModel):
    domain: str
    payload: dict[str, Any]
    confirm: bool = False


class ApplyResponse(BaseModel):
    applied: bool
    message: str
    live_reloaded: bool = False


def _domain_schema(domain: str) -> type[BaseModel] | None:
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesConfig,
        McpServersConfig,
        ModelConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
    )

    mapping: dict[str, type[BaseModel]] = {
        "app": AppConfig,
        "telegram": TelegramChannelConfig,
        "model": ModelConfig,
        "capabilities": CapabilitiesConfig,
        "mcp_servers": McpServersConfig,
        "scheduler": SchedulerConfig,
        "store": StoreConfig,
    }
    return mapping.get(domain)


def _current_domain_dict(runtime_config: RuntimeConfig, domain: str) -> dict[str, Any]:
    domain_obj = getattr(runtime_config, domain, None)
    if domain_obj is None:
        return {}
    result: dict[str, Any] = domain_obj.model_dump()
    return result


@router.get(
    "/effective",
    response_model=EffectiveConfigResponse,
    dependencies=[AdminAuthDep],
    summary="Get effective config with redaction and provenance",
)
async def get_effective_config() -> EffectiveConfigResponse:
    """Returns the full effective config freshly read from disk and env overrides.

    Sensitive values are redacted. Each field includes a provenance label:
    'file', 'env_override', or 'default'.
    """
    loader = ConfigLoader()
    result = loader.effective_config()
    return EffectiveConfigResponse(
        config=result["config"],
        provenance=result["provenance"],
    )


@router.post(
    "/validate",
    response_model=ValidateResponse,
    dependencies=[AdminAuthDep],
    summary="Validate a candidate config payload for a domain",
)
async def validate_config(body: ValidateRequest) -> ValidateResponse:
    """Validates a proposed config payload against the domain schema."""
    schema_cls = _domain_schema(body.domain)
    if schema_cls is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown config domain: {body.domain}",
        )
    try:
        schema_cls(**body.payload)
        return ValidateResponse(valid=True, errors=[])
    except ValidationError as exc:
        errors = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return ValidateResponse(valid=False, errors=errors)


@router.post(
    "/diff",
    response_model=DiffResponse,
    dependencies=[AdminAuthDep],
    summary="Preview diff between current and proposed config",
)
async def diff_config(
    runtime_config: RuntimeConfigDep,
    body: ValidateRequest,
) -> DiffResponse:
    """Returns changed keys between current domain config and proposed payload."""
    current = _current_domain_dict(runtime_config, body.domain)
    allowed = _ALLOWLISTED_KEYS.get(body.domain, set())
    changes: list[DiffEntry] = []
    blocked: list[str] = []

    for key, proposed_val in body.payload.items():
        if key not in allowed:
            blocked.append(key)
            continue
        current_val = current.get(key)
        if current_val != proposed_val:
            changes.append(DiffEntry(key=key, current=current_val, proposed=proposed_val))

    return DiffResponse(changes=changes, blocked_keys=blocked)


@router.post(
    "/apply",
    response_model=ApplyResponse,
    dependencies=[AdminAuthDep],
    summary="Apply validated config changes to disk and reload live runtime state",
)
async def apply_config(
    runtime_config: RuntimeConfigDep,
    body: ApplyRequest,
) -> ApplyResponse:
    """Applies allowlisted config changes to the YAML file after validation.

    Requires confirm=true. Non-allowlisted keys are silently ignored.
    After a successful disk write, reloads the domain into the live runtime config.
    """
    if not body.confirm:
        return ApplyResponse(
            applied=False,
            message="Set confirm=true to apply changes after reviewing the diff.",
        )

    schema_cls = _domain_schema(body.domain)
    if schema_cls is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown config domain: {body.domain}",
        )

    allowed = _ALLOWLISTED_KEYS.get(body.domain, set())
    current = _current_domain_dict(runtime_config, body.domain)
    filtered = {k: v for k, v in body.payload.items() if k in allowed}

    if not filtered:
        return ApplyResponse(applied=False, message="No allowlisted keys in payload.")

    merged = {**current, **filtered}
    try:
        schema_cls(**merged)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Validation failed", "errors": errors},
        ) from exc

    filename = _FILENAME_MAP.get(body.domain)
    if filename is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown domain")

    config_path = resolve_config_dir() / filename
    tmp_path = config_path.with_suffix(".yaml.tmp")
    try:
        with open(tmp_path, "w") as f:
            yaml.dump(merged, f, default_flow_style=False)
        tmp_path.replace(config_path)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write config: {exc}",
        ) from exc

    # Reload the updated domain from disk+env and update live runtime state.
    live_reloaded = False
    new_domain_obj = ConfigLoader().reload_domain(body.domain)
    if new_domain_obj is not None:
        update_runtime_config_domain(body.domain, new_domain_obj)
        live_reloaded = True

    applied_keys = list(filtered.keys())
    return ApplyResponse(
        applied=True,
        live_reloaded=live_reloaded,
        message=f"Applied {len(applied_keys)} key(s) to {body.domain}: {applied_keys}",
    )
