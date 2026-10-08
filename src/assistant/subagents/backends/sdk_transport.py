"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

claude-agent-sdk subprocess transport that shuts down cleanly when a run is
cancelled or times out mid-stream.

Reaches into the SDK's private transport module (as claude_code.py does for
the message parser), so claude_code_streaming.py imports it under its
_SDK_AVAILABLE guard.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ProcessError
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport


class QuietCloseTransport(SubprocessCLITransport):
    """SubprocessCLITransport without the SDK's two abandoned-stream defects.

    Both show up only when the query is closed while the CLI is still running
    (cancel_task, run timeout), never on a run that ends by itself:

    * The SDK's reader task can be cancelled between two messages of one stdout
      chunk, leaving the read_messages() generator suspended at its ``yield``
      and unreferenced.  asyncio then finalizes it in a task of its own, where
      the generator swallows GeneratorExit, waits for the CLI the SDK is about
      to SIGTERM and raises ProcessError(143) with nobody to catch it:
      "Task exception was never retrieved".
    * close() waits for the CLI to exit without reading its stdout.  A CLI that
      keeps streaming fills the pipe, and asyncio does not report a process as
      exited until its pipes hit EOF, so close() - and the cancellation waiting
      on it - never returns.
    """

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read_messages_quietly()

    async def _read_messages_quietly(self) -> AsyncIterator[dict[str, Any]]:
        inner = super().read_messages()
        try:
            async for message in inner:
                yield message
        except GeneratorExit:
            # Abandoned by the reader: the exit code is the SIGTERM we sent,
            # not a failure anyone is waiting to hear about.
            with contextlib.suppress(ProcessError):
                await inner.aclose()  # type: ignore[attr-defined]
            raise

    async def close(self) -> None:
        drain = asyncio.ensure_future(self._drain_stdout())
        try:
            await super().close()
        finally:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain

    async def _drain_stdout(self) -> None:
        stdout = self._stdout_stream
        if stdout is None:
            return
        # Ends on EOF, or on whatever closing the stream under us raises.
        with contextlib.suppress(Exception):
            async for _ in stdout:
                pass
