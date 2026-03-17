"""Tests for delegate_subagent_task tool."""

import pytest

from assistant.agent.deps import TurnDeps
from assistant.agent.tools.delegate_subagent_task import delegate_subagent_task


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
    async def _handler(payload: dict) -> dict:
        return {
            "accepted": True,
            "task_id": "dlg-1",
            "status": "pending",
            "payload_echo": payload,
        }

    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        delegation_enqueue_handler=_handler,
        tool_runtime_params={
            "delegate_subagent_task": {"delegation_allowed_backends": ["claude_code"]}
        },
    )
    ctx = _Ctx(deps)
    result = await delegate_subagent_task(
        ctx,
        objective="Implement feature",
        workflow_id="coding",
        metadata={"ticket": "PC-1"},
    )
    assert result["accepted"] is True
    assert result["task_id"] == "dlg-1"
    payload = result["payload_echo"]
    assert payload["objective"] == "Implement feature"
    assert payload["workflow_id"] == "coding"
    assert payload["metadata"]["ticket"] == "PC-1"
