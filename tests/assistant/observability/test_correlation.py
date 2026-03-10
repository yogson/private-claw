"""Tests for correlation ID context variable utilities."""

import asyncio

import pytest

from assistant.observability.correlation import (
    generate_trace_id,
    get_trace_id,
    reset_trace_id,
    set_trace_id,
)


def test_generate_trace_id_returns_nonempty_string() -> None:
    tid = generate_trace_id()
    assert isinstance(tid, str)
    assert len(tid) == 32  # UUID4 hex


def test_generate_trace_id_unique() -> None:
    assert generate_trace_id() != generate_trace_id()


def test_set_and_get_trace_id() -> None:
    token = set_trace_id("abc123")
    try:
        assert get_trace_id() == "abc123"
    finally:
        reset_trace_id(token)


def test_get_trace_id_auto_generates_when_unset() -> None:
    # Ensure no trace ID is set by running in a fresh task
    result: list[str] = []

    async def _inner() -> None:
        result.append(get_trace_id())

    asyncio.run(_inner())
    assert len(result[0]) == 32


def test_reset_trace_id_restores_previous() -> None:
    outer_token = set_trace_id("outer")
    inner_token = set_trace_id("inner")
    assert get_trace_id() == "inner"
    reset_trace_id(inner_token)
    assert get_trace_id() == "outer"
    reset_trace_id(outer_token)


def test_set_trace_id_isolated_across_tasks() -> None:
    collected: list[str] = []

    async def _task(tid: str) -> None:
        token = set_trace_id(tid)
        await asyncio.sleep(0)
        collected.append(get_trace_id())
        reset_trace_id(token)

    async def _main() -> None:
        await asyncio.gather(_task("task-A"), _task("task-B"))

    asyncio.run(_main())
    assert set(collected) == {"task-A", "task-B"}


@pytest.mark.asyncio
async def test_correlation_propagated_through_await() -> None:
    token = set_trace_id("propagated")
    await asyncio.sleep(0)
    assert get_trace_id() == "propagated"
    reset_trace_id(token)
