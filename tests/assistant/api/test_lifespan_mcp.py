"""MCP startup must not leak when a later lifespan init step fails."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from tests.assistant.api.test_telegram_lifecycle import _runtime_config

from assistant.api.lifespan import _lifespan


async def test_failed_init_does_not_leave_mcp_started_without_cleanup() -> None:
    config = _runtime_config(allowlist=[1])
    config.memory.api_key = ""  # init raises SystemExit after the store is set up
    fake = MagicMock()
    fake.start = AsyncMock(return_value={})
    fake.close = AsyncMock()
    store = MagicMock()
    store.initialize = AsyncMock()
    with (
        patch("assistant.api.lifespan.bootstrap", return_value=config),
        patch("assistant.api.lifespan.configure_logging", return_value="x.log"),
        patch("assistant.api.lifespan.configure_logfire", return_value=False),
        patch("assistant.api.lifespan.StoreFacade", return_value=store),
        patch("assistant.api.lifespan.mcp_toolsets", fake),
        patch("assistant.api.lifespan.collect_enabled_mcp_server_ids", return_value=["s"]),
    ):
        with pytest.raises(SystemExit):
            async with _lifespan(FastAPI()):
                pass  # pragma: no cover
    # Either MCP was never started, or it was started and closed; never started and leaked.
    assert fake.start.call_count == 0 or fake.close.await_count >= 1
