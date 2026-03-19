"""Tests for delegate_subagent_task tool."""

import pytest

from assistant.agent.deps import TurnDeps
from assistant.agent.tools.delegate_subagent_task import delegate_subagent_task

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_handler():
    async def _handler(payload: dict) -> dict:
        return {
            "accepted": True,
            "task_id": "dlg-1",
            "status": "pending",
            "payload_echo": payload,
        }

    return _handler


def _make_deps(**kwargs):
    return TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        delegation_enqueue_handler=_make_handler(),
        tool_runtime_params={
            "delegate_subagent_task": {"delegation_allowed_backends": ["claude_code"]}
        },
        **kwargs,
    )


class _Ctx:
    def __init__(self, deps: TurnDeps) -> None:
        self.deps = deps


@pytest.mark.asyncio
async def test_delegate_subagent_task_returns_unavailable_without_handler() -> None:
    deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
    ctx = _Ctx(deps)
    result = await delegate_subagent_task(ctx, objective="Implement feature")
    assert result["accepted"] is False
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_delegate_subagent_task_calls_handler() -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Implement feature",
        model_id="claude-sonnet-4-5",
    )
    assert result["accepted"] is True
    assert result["task_id"] == "dlg-1"
    payload = result["payload_echo"]
    assert payload["objective"] == "Implement feature"
    assert payload["model_id"] == "claude-sonnet-4-5"
    assert payload["tool_params"]["delegation_allowed_backends"] == ["claude_code"]
    assert payload["backend_params"] == {}


@pytest.mark.asyncio
async def test_delegate_subagent_task_directory_forwarded(tmp_path) -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Implement feature",
        directory=str(tmp_path),
    )
    assert result["accepted"] is True
    payload = result["payload_echo"]
    assert payload["backend_params"] == {"directory": str(tmp_path)}


@pytest.mark.asyncio
async def test_delegate_subagent_task_no_directory_empty_backend_params() -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(ctx, objective="Implement feature")
    assert result["accepted"] is True
    payload = result["payload_echo"]
    assert payload["backend_params"] == {}


@pytest.mark.asyncio
async def test_delegate_subagent_task_invalid_directory_returns_error() -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Implement feature",
        directory="/nonexistent/path/that/cannot/exist",
    )
    assert result["accepted"] is False
    assert result["status"] == "error"
    assert "directory does not exist" in result["rejection_reason"]
