"""
Component ID: CMP_OBSERVABILITY_LOGGING

Logfire bootstrap for Pydantic AI instrumentation.
"""

from collections.abc import Callable
from typing import Any

import structlog

from assistant.core.config.schemas import AppConfig

logger = structlog.get_logger(__name__)


def configure_logfire(
    app_config: AppConfig,
    *,
    configure_fn: Callable[..., Any] | None = None,
    instrument_fn: Callable[[], Any] | None = None,
) -> bool:
    """Configure Logfire and instrument Pydantic AI."""
    if configure_fn is None or instrument_fn is None:
        try:
            import logfire
        except Exception as exc:  # pragma: no cover - import error path
            logger.warning("logfire.unavailable", error=str(exc))
            return False
        configure_fn = configure_fn or logfire.configure
        instrument_fn = instrument_fn or logfire.instrument_pydantic_ai

    try:
        token = app_config.logfire_token.strip()
        if token:
            configure_fn(token=token)
            logger.info("Logfire cloud logging enabled")
        else:
            # Console-only mode with local traces/logs and no cloud shipping.
            configure_fn(send_to_logfire=False)
        instrument_fn()
    except Exception as exc:
        logger.warning("logfire.init_failed", error=str(exc))
        return False

    return True
