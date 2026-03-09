"""
Component ID: CMP_API_FASTAPI_GATEWAY

Shared FastAPI dependencies: runtime config access and admin auth guard.
"""

import os
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from assistant.core.config.schemas import RuntimeConfig

_bearer_scheme = HTTPBearer(auto_error=False)

_runtime_config: RuntimeConfig | None = None


def set_runtime_config(config: RuntimeConfig) -> None:
    """Register the loaded RuntimeConfig for dependency injection."""
    global _runtime_config
    _runtime_config = config


def update_runtime_config_domain(domain: str, domain_value: Any) -> None:
    """Replace a single domain in the live RuntimeConfig after a successful config apply."""
    global _runtime_config
    if _runtime_config is None:
        return
    _runtime_config = _runtime_config.model_copy(update={domain: domain_value})


def get_runtime_config() -> RuntimeConfig:
    """FastAPI dependency: returns the loaded RuntimeConfig."""
    if _runtime_config is None:
        raise RuntimeError("RuntimeConfig not initialised; call set_runtime_config first")
    return _runtime_config


RuntimeConfigDep = Annotated[RuntimeConfig, Depends(get_runtime_config)]


def _verify_admin_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    admin_token = os.environ.get("ASSISTANT_ADMIN_TOKEN", "")
    if not admin_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin token not configured",
        )
    if credentials is None or credentials.credentials != admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin token",
            headers={"WWW-Authenticate": "Bearer"},
        )


AdminAuthDep = Depends(_verify_admin_token)
