"""Tests for check_subagent_status tool."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from assistant.agent.deps import TurnDeps
from assistant.agent.tools.check_subagent_status import check_subagent_status


def _make_handler(response: dict[str, Any]) -> Callable[[str], Awaitable[dict[str, Any]]]:
    async def _handler(task_id: str) -> dict[str, Any]:
        return {**response, "task_id": task_id}

    return _handler


def _make_deps(handler: Callable[[str], Awaitable[dict[str, Any]]] | None) -> TurnDeps:
    return TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        delegation_status_handler=handler,
    )


class _Ctx:
    def __init__(self, deps: TurnDeps) -> None:
        self.deps = deps


@pytest.mark.asyncio
async def test_check_subagent_status_returns_unavailable_without_handler() -> None:
    ctx = _Ctx(_make_deps(None))
    result = await check_subagent_status(ctx, task_id="dlg-1")
    assert result["found"] is False
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_check_subagent_status_calls_handler_with_task_id() -> None:
    handler = _make_handler({"found": True, "status": "running"})
    ctx = _Ctx(_make_deps(handler))
    result = await check_subagent_status(ctx, task_id="dlg-1")
    assert result["found"] is True
    assert result["status"] == "running"
    assert result["task_id"] == "dlg-1"


@pytest.mark.asyncio
async def test_check_subagent_status_surfaces_not_found() -> None:
    handler = _make_handler({"found": False})
    ctx = _Ctx(_make_deps(handler))
    result = await check_subagent_status(ctx, task_id="unknown")
    assert result["found"] is False
