"""Tests for check_subagent_status tool."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from assistant.agent.deps import MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN, TurnDeps
from assistant.agent.tools.check_subagent_status import check_subagent_status
from assistant.agent.tools.read_subagent_log import read_subagent_log


def _make_handler(response: dict[str, Any]) -> Callable[[str], Awaitable[dict[str, Any]]]:
    async def _handler(task_id: str) -> dict[str, Any]:
        return {**response, "task_id": task_id}

    return _handler


def _make_deps(
    handler: Callable[[str], Awaitable[dict[str, Any]]] | None,
    log_handler: Callable[[str, int], Awaitable[dict[str, Any]]] | None = None,
) -> TurnDeps:
    return TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        delegation_status_handler=handler,
        delegation_log_handler=log_handler,
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


@pytest.mark.asyncio
async def test_check_subagent_status_refuses_after_poll_budget_exhausted() -> None:
    """Regression test: an unbounded poll loop on a slow delegated task has
    been observed burning through pydantic-ai's request_limit and crashing
    the whole turn with UsageLimitExceeded. The per-task, per-turn budget
    must hard-stop further calls well before that."""
    handler = _make_handler({"found": True, "status": "running"})
    deps = _make_deps(handler)
    ctx = _Ctx(deps)

    for _ in range(MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN):
        result = await check_subagent_status(ctx, task_id="dlg-1")
        assert result["status"] == "running"

    refused = await check_subagent_status(ctx, task_id="dlg-1")
    assert refused["status"] == "poll_limit_reached"
    assert refused["found"] is False


@pytest.mark.asyncio
async def test_check_subagent_status_poll_budget_is_per_task_id() -> None:
    handler = _make_handler({"found": True, "status": "running"})
    deps = _make_deps(handler)
    ctx = _Ctx(deps)

    for _ in range(MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN):
        await check_subagent_status(ctx, task_id="dlg-1")

    # A different task_id has its own independent budget.
    other = await check_subagent_status(ctx, task_id="dlg-2")
    assert other["status"] == "running"


@pytest.mark.asyncio
async def test_poll_budget_is_shared_between_status_and_log_tools() -> None:
    status_handler = _make_handler({"found": True, "status": "running"})

    async def _log_handler(task_id: str, tail_lines: int) -> dict[str, Any]:
        return {"found": True, "status": "running", "lines": [], "task_id": task_id}

    deps = _make_deps(status_handler, log_handler=_log_handler)
    ctx = _Ctx(deps)

    calls = [check_subagent_status, read_subagent_log]
    for i in range(MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN):
        tool = calls[i % len(calls)]
        result = await tool(ctx, task_id="dlg-1")
        assert result["status"] == "running"

    # Budget already spent across the two different tools for this task_id.
    refused = await check_subagent_status(ctx, task_id="dlg-1")
    assert refused["status"] == "poll_limit_reached"
    refused_log = await read_subagent_log(ctx, task_id="dlg-1")
    assert refused_log["status"] == "poll_limit_reached"
