"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Media-group (album) aggregation buffer for Telegram ingress.

Telegram delivers each photo/document in an album as a separate Update
that shares the same media_group_id.  This buffer collects all such
message dicts for a short window and then fires a single flush callback
with the complete ordered list so the caller can build one aggregated
NormalizedEvent instead of N independent single-file events.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_FLUSH_DELAY_SECONDS: float = 1.0


@dataclass
class _GroupState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    flush_task: asyncio.Task[None] | None = None


class MediaGroupBuffer:
    """
    Collects Telegram media-group messages and flushes them as a batch.

    When a message dict is added via add():
    - It is appended to the buffer for that media_group_id.
    - Any pending flush timer for the group is cancelled and a new one
      is started.
    - After flush_delay_seconds elapse with no new messages for that
      group the flush_callback is invoked with the ordered list of
      message dicts.

    All exceptions raised by flush_callback are caught and logged so
    that a faulty album cannot stall the surrounding polling loop.
    """

    def __init__(
        self,
        flush_callback: Callable[[list[dict[str, Any]]], Awaitable[None]],
        flush_delay_seconds: float = _DEFAULT_FLUSH_DELAY_SECONDS,
    ) -> None:
        self._callback = flush_callback
        self._delay = flush_delay_seconds
        self._groups: dict[str, _GroupState] = {}

    async def add(self, media_group_id: str, message: dict[str, Any]) -> None:
        """Add a message dict to the named media group and reschedule the flush."""
        state = self._groups.setdefault(media_group_id, _GroupState())
        state.messages.append(message)

        if state.flush_task is not None and not state.flush_task.done():
            state.flush_task.cancel()

        state.flush_task = asyncio.create_task(self._flush_after_delay(media_group_id))

    async def _flush_after_delay(self, media_group_id: str) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return

        state = self._groups.pop(media_group_id, None)
        if state is None or not state.messages:
            return

        logger.debug(
            "telegram.media_group.flushing",
            media_group_id=media_group_id,
            count=len(state.messages),
        )

        try:
            await self._callback(state.messages)
        except Exception:
            logger.exception(
                "telegram.media_group.flush_error",
                media_group_id=media_group_id,
            )
