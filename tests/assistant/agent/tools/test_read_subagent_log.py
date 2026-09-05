"""Tests for read_subagent_log tool."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from assistant.agent.deps import TurnDeps
from assistant.agent.tools.read_subagent_log import read_subagent_log


def _make_handler(
    response: dict[str, Any],
) -> Callable[[str, int], Awaitable[dict[str, Any]]]:
    calls: list[tuple[str, int]] = []

    async def _handler(task_id: str, tail_lines: int) -> dict[str, Any]:
        calls.append((task_id, tail_lines))
        return {**response, "task_id": task_id}

    _handler.calls = calls  # type: ignore[attr-defined]
    return _handler


def _make_deps(handler: Callable[[str, int], Awaitable[dict[str, Any]]] | None) -> TurnDeps:
    return TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        delegation_log_handler=handler,
    )


class _Ctx:
    def __init__(self, deps: TurnDeps) -> None:
        self.deps = deps


@pytest.mark.asyncio
async def test_read_subagent_log_returns_unavailable_without_handler() -> None:
    ctx = _Ctx(_make_deps(None))
    result = await read_subagent_log(ctx, task_id="dlg-1")
    assert result["found"] is False
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_read_subagent_log_calls_handler_with_task_id_and_tail_lines() -> None:
    handler = _make_handler({"found": True, "status": "running", "lines": ["[assistant] hi"]})
    ctx = _Ctx(_make_deps(handler))
    result = await read_subagent_log(ctx, task_id="dlg-1", tail_lines=50)
    assert result["found"] is True
    assert result["lines"] == ["[assistant] hi"]
    assert handler.calls == [("dlg-1", 50)]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_subagent_log_default_tail_lines_is_200() -> None:
    handler = _make_handler({"found": True, "status": "pending", "lines": []})
    ctx = _Ctx(_make_deps(handler))
    await read_subagent_log(ctx, task_id="dlg-1")
    assert handler.calls == [("dlg-1", 200)]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_subagent_log_surfaces_not_found() -> None:
    handler = _make_handler({"found": False})
    ctx = _Ctx(_make_deps(handler))
    result = await read_subagent_log(ctx, task_id="unknown")
    assert result["found"] is False
