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
    "capabilities": {"enabled_capabilities", "denied_capabilities"},
    "tools": {"tools"},
    "scheduler": {"tick_seconds", "max_lateness_seconds", "max_jobs"},
    "store": {"lock_ttl_seconds", "idempotency_retention_seconds"},
    "memory": {"default_user_id", "org_id", "project_id"},
}

_FILENAME_MAP: dict[str, str] = {
    "app": "app.yaml",
    "telegram": "channel.telegram.yaml",
    "model": "model.yaml",
    "capabilities": "capabilities.yaml",
    "tools": "tools.yaml",
    "mcp_servers": "mcp_servers.yaml",
    "scheduler": "scheduler.yaml",
    "store": "store.yaml",
    "memory": "memory.yaml",
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
        CapabilitiesPolicyConfig,
        McpServersConfig,
        MemoryConfig,
        ModelConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolsConfig,
    )

    mapping: dict[str, type[BaseModel]] = {
        "app": AppConfig,
        "telegram": TelegramChannelConfig,
        "model": ModelConfig,
        "capabilities": CapabilitiesPolicyConfig,
        "tools": ToolsConfig,
        "mcp_servers": McpServersConfig,
        "scheduler": SchedulerConfig,
        "store": StoreConfig,
        "memory": MemoryConfig,
    }
    return mapping.get(domain)


def _read_domain_file_or_raise(loader: ConfigLoader, domain: str) -> dict[str, Any]:
    """Return the domain's own YAML keys (no env overrides); {} if the file is absent."""
    raw = loader.read_domain_file(domain)
    if raw is not None:
        return raw
    filename = _FILENAME_MAP.get(domain)
    if filename is not None and (resolve_config_dir() / filename).exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cannot read existing config file {filename}; fix it on disk first.",
        )
    return {}


def _file_value(raw: dict[str, Any], schema_cls: type[BaseModel] | None, key: str) -> Any:
    """Value of ``key`` as stored in the file, or the schema default when the file omits it."""
    if key in raw:
        return raw[key]
    field = schema_cls.model_fields.get(key) if schema_cls is not None else None
    if field is None or field.is_required():
        return None
    return field.get_default(call_default_factory=True)


def editable_file_values(domain: str) -> dict[str, Any]:
    """The domain's allowlisted keys as its YAML file stores them, without env overrides.

    Keys the file omits get the schema default (None if required). This is what an edit
    form should start from: submitting it unchanged writes nothing new to the file.
    Raises HTTPException if the file exists but cannot be read.
    """
    raw = _read_domain_file_or_raise(ConfigLoader(), domain)
    schema_cls = _domain_schema(domain)
    return {key: _file_value(raw, schema_cls, key) for key in _ALLOWLISTED_KEYS.get(domain, ())}


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
    """Returns changed keys between the domain's YAML file and the proposed payload.

    ``current`` is the value stored in the file (or the schema default), not the live
    value: env overrides are never written, so they are not part of the diff either.
    """
    del runtime_config  # kept for API compatibility; the diff is computed against the file
    raw = _read_domain_file_or_raise(ConfigLoader(), body.domain)
    schema_cls = _domain_schema(body.domain)
    allowed = _ALLOWLISTED_KEYS.get(body.domain, set())
    changes: list[DiffEntry] = []
    blocked: list[str] = []

    for key, proposed_val in body.payload.items():
        if key not in allowed:
            blocked.append(key)
            continue
        current_val = _file_value(raw, schema_cls, key)
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
    The payload is merged into the file's own keys (read from disk), never into the
    live config, so env-only overrides (e.g. ``ASSISTANT_APP_DATA_ROOT`` or the bot
    token) are not persisted to the shared YAML. The result is validated with the
    current env overrides applied. After a successful disk write, reloads the domain
    into the live runtime config.
    """
    del runtime_config  # kept for API compatibility; the merge base is the file on disk
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
    filtered = {k: v for k, v in body.payload.items() if k in allowed}

    if not filtered:
        return ApplyResponse(applied=False, message="No allowlisted keys in payload.")

    loader = ConfigLoader()
    file_data = _read_domain_file_or_raise(loader, body.domain)
    merged = {**file_data, **filtered}
    try:
        schema_cls(**loader.apply_domain_env_overrides(body.domain, merged))
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
    new_domain_obj = loader.reload_domain(body.domain)
    if new_domain_obj is not None:
        update_runtime_config_domain(body.domain, new_domain_obj)
        live_reloaded = True

    applied_keys = list(filtered.keys())
    return ApplyResponse(
        applied=True,
        live_reloaded=live_reloaded,
        message=f"Applied {len(applied_keys)} key(s) to {body.domain}: {applied_keys}",
    )
