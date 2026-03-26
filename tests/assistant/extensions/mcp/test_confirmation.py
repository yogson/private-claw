"""Tests for MCP confirmation gate (P4)."""

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from assistant.extensions.mcp.confirmation import McpConfirmationDenied, check_confirmation


@dataclass
class _FakeDeps:
    """Minimal TurnDeps stand-in for confirmation tests."""

    writes_approved: list[None] = field(default_factory=list)
    seen_intent_ids: set[str] = field(default_factory=set)
    memory_search_handler: Any = None
    delegation_enqueue_handler: Any = None
    tool_runtime_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_call_notifier: Any = None


@dataclass
class _FakeCtx:
    """Minimal RunContext stand-in."""

    deps: _FakeDeps


@pytest.mark.asyncio
async def test_check_confirmation_proceeds_without_notifier() -> None:
    """When no notifier is set, confirmation is fail-open."""
    ctx = _FakeCtx(deps=_FakeDeps(tool_call_notifier=None))
    # Should not raise
    await check_confirmation(
        ctx,  # type: ignore[arg-type]
        "cap.mcp.test.tool",
        "test",
        "tool",
        {"arg": "value"},
    )


@pytest.mark.asyncio
async def test_check_confirmation_calls_notifier() -> None:
    """When notifier is set, it is called with capability_id and JSON payload."""
    notifier = AsyncMock()
    ctx = _FakeCtx(deps=_FakeDeps(tool_call_notifier=notifier))
    await check_confirmation(
        ctx,  # type: ignore[arg-type]
        "cap.mcp.test.tool",
        "test",
        "tool",
        {"url": "https://example.com"},
    )
    notifier.assert_awaited_once()
    call_args = notifier.call_args[0]
    assert call_args[0] == "cap.mcp.test.tool"
    assert "mcp_confirmation_required" in call_args[1]
    assert "https://example.com" in call_args[1]


@pytest.mark.asyncio
async def test_check_confirmation_notifier_can_raise() -> None:
    """Notifier raising McpConfirmationDenied propagates to caller."""

    async def _deny(cap_id: str, payload: str) -> None:
        raise McpConfirmationDenied(cap_id, "test", "tool")

    ctx = _FakeCtx(deps=_FakeDeps(tool_call_notifier=_deny))
    with pytest.raises(McpConfirmationDenied):
        await check_confirmation(
            ctx,  # type: ignore[arg-type]
            "cap.mcp.test.tool",
            "test",
            "tool",
            {},
        )


def test_mcp_confirmation_denied_message() -> None:
    exc = McpConfirmationDenied("cap.mcp.test.tool", "test", "tool")
    assert "tool" in str(exc)
    assert "test" in str(exc)
    assert exc.capability_id == "cap.mcp.test.tool"
