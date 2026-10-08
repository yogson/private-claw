"""Tests for QuietCloseTransport."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ProcessError
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from assistant.subagents.backends.sdk_transport import QuietCloseTransport


def _make_transport() -> QuietCloseTransport:
    # Skip __init__: it resolves a CLI binary, which these tests never spawn.
    return QuietCloseTransport.__new__(QuietCloseTransport)


def _sdk_read_messages(messages: list[dict[str, Any]]) -> Any:
    """Stand-in for the SDK's read_messages(): swallows GeneratorExit, then
    reports the exit code of the CLI the SDK terminated."""

    async def _read(self: Any) -> AsyncGenerator[dict[str, Any], None]:
        try:
            for message in messages:
                yield message
        except GeneratorExit:
            pass
        raise ProcessError("Command failed with exit code 143", exit_code=143)

    return _read


@pytest.mark.asyncio
async def test_abandoned_read_messages_does_not_raise_on_finalization() -> None:
    with patch.object(SubprocessCLITransport, "read_messages", _sdk_read_messages([{"n": 1}])):
        stream = _make_transport().read_messages()
        assert await anext(stream) == {"n": 1}
        # What asyncio's async-generator finalizer does to an abandoned stream.
        await stream.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_messages_still_raises_when_cli_fails_by_itself() -> None:
    with patch.object(SubprocessCLITransport, "read_messages", _sdk_read_messages([{"n": 1}])):
        stream = _make_transport().read_messages()
        with pytest.raises(ProcessError):
            async for _ in stream:
                pass


@pytest.mark.asyncio
async def test_close_drains_stdout_while_waiting_for_the_cli_to_exit() -> None:
    drained = asyncio.Event()

    async def _stdout() -> AsyncGenerator[str, None]:
        yield "left in the pipe"
        drained.set()

    async def _sdk_close(self: Any) -> None:
        # The CLI can only exit once someone has read what it wrote.
        await asyncio.wait_for(drained.wait(), timeout=2)

    transport = _make_transport()
    transport._stdout_stream = _stdout()  # type: ignore[assignment]
    with patch.object(SubprocessCLITransport, "close", _sdk_close):
        await transport.close()
