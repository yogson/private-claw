"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Coalescing of long texts that the Telegram client split into several messages.

Telegram caps a text message at 4096 UTF-16 code units. When a user pastes a
longer text, the client sends it as consecutive messages (each part close to
the limit, the last one usually shorter), and the bot receives one update per
part. Without coalescing each part becomes its own turn that sees only a
fragment of the text.

The buffer is applied to raw updates, before they are normalized into events:

- Only a plain text message from a sender with no pending buffer that is long
  enough to be a split part (``SPLIT_PART_MIN_UTF16``) opens a buffer. Every
  other message is dispatched at once, so short messages get no added delay.
- While a buffer is open, the next plain text message from the same chat and
  user is appended. A long part restarts the window (more parts may follow);
  a short part is the tail of the split, so the buffer is flushed at once.
- The buffer is also flushed when the window expires, and before any other
  message from the same chat and user (command, attachment, forward, ...) is
  dispatched, so arrival order is kept and ``/stop`` is never held.
- ``flush_all()`` cancels every timer and flushes every buffer (shutdown).

A flush hands one synthetic update (first part's metadata, joined text) to
the flush callback, together with diagnostics metadata for the event.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

TELEGRAM_TEXT_LIMIT_UTF16 = 4096
# Telegram Desktop cuts a long text at the best break (paragraph, newline, space)
# found in the second half of the limit, so a non-final part is never shorter
# than half of it. Shorter texts can not be a non-final split part.
SPLIT_PART_MIN_UTF16 = TELEGRAM_TEXT_LIMIT_UTF16 // 2
# Observed in production: the client sends part 1, then the remaining parts
# about 2.2 s later (and a few ms apart). The window must cover that gap.
SPLIT_WINDOW_SECONDS = 3.0

# Messages with any of these keys are never merged (not a client-side split).
_NON_SPLIT_KEYS = ("media_group_id", "forward_origin", "forward_date", "via_bot")

SplitFlushCallback = Callable[[dict[str, Any], dict[str, Any]], None]
"""Receives the merged update and diagnostics metadata for the resulting event."""


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the unit of Telegram's message limit."""
    return len(text.encode("utf-16-le")) // 2


def _sender_key(message: dict[str, Any]) -> tuple[int, int] | None:
    user_id = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id", user_id)
    if user_id is None or chat_id is None:
        return None
    return int(chat_id), int(user_id)


def _mergeable_text(message: dict[str, Any]) -> str | None:
    """Return the text of a plain, non-command text message; ``None`` otherwise."""
    text = message.get("text")
    if not isinstance(text, str) or not text or text.startswith("/"):
        return None
    if any(key in message for key in _NON_SPLIT_KEYS):
        return None
    return text


def _join(parts: list[str]) -> str:
    """Join split parts; add a newline only where the cut swallowed the whitespace."""
    joined = parts[0]
    for part in parts[1:]:
        if joined[-1:].isspace() or part[:1].isspace():
            joined += part
        else:
            joined += "\n" + part
    return joined


@dataclass
class _Buffer:
    started_at: float
    entries: list[tuple[int | None, dict[str, Any]]] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None

    def message_ids(self) -> set[Any]:
        return {message.get("message_id") for _, message in self.entries}


class SplitMessageCoalescer:
    """Merges the parts of a Telegram-split long text into one update per sender."""

    def __init__(
        self,
        flush_callback: SplitFlushCallback,
        *,
        window_seconds: float | None = None,
        min_part_utf16: int | None = None,
    ) -> None:
        self._callback = flush_callback
        self._window = SPLIT_WINDOW_SECONDS if window_seconds is None else window_seconds
        self._min_part = SPLIT_PART_MIN_UTF16 if min_part_utf16 is None else min_part_utf16
        self._buffers: dict[tuple[int, int], _Buffer] = {}

    @property
    def pending(self) -> int:
        """Number of senders with an open buffer."""
        return len(self._buffers)

    def offer(self, update: dict[str, Any]) -> bool:
        """Route one update through the buffer.

        Returns True when the update was consumed (buffered, merged or dropped as a
        duplicate part). Returns False when the caller must dispatch it as usual; a
        pending buffer of the same sender is flushed first, so order is kept.
        """
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        key = _sender_key(message)
        if key is None:
            return False
        buffer = self._buffers.get(key)
        text = _mergeable_text(message)
        if text is None:
            if buffer is not None:
                self._flush(key, reason="other_message")
            return False

        is_long = utf16_len(text) >= self._min_part
        if buffer is None:
            if not is_long:
                return False
            buffer = _Buffer(started_at=asyncio.get_running_loop().time())
            self._buffers[key] = buffer
        elif message.get("message_id") is not None and (
            message.get("message_id") in buffer.message_ids()
        ):
            logger.info(
                "telegram.split_text.duplicate_part",
                chat_id=key[0],
                message_id=message.get("message_id"),
                update_id=update.get("update_id"),
            )
            return True

        buffer.entries.append((update.get("update_id"), message))
        if buffer.timer is not None:
            buffer.timer.cancel()
            buffer.timer = None
        if is_long:
            buffer.timer = asyncio.get_running_loop().call_later(
                self._window, self._on_timer, key, buffer
            )
        else:
            self._flush(key, reason="tail")
        return True

    def flush_all(self) -> None:
        """Cancel every timer and flush every open buffer (used on shutdown)."""
        for key in list(self._buffers):
            self._flush(key, reason="shutdown")

    def _on_timer(self, key: tuple[int, int], buffer: _Buffer) -> None:
        if self._buffers.get(key) is buffer:
            self._flush(key, reason="window")

    def _flush(self, key: tuple[int, int], *, reason: str) -> None:
        buffer = self._buffers.pop(key, None)
        if buffer is None or not buffer.entries:
            return
        if buffer.timer is not None:
            buffer.timer.cancel()
            buffer.timer = None

        entries = sorted(buffer.entries, key=lambda e: e[1].get("message_id") or 0)
        first_update_id, first = entries[0]
        merged = {k: v for k, v in first.items() if k != "entities"}
        merged["text"] = _join([message["text"] for _, message in entries])
        update: dict[str, Any] = {"message": merged}
        if first_update_id is not None:
            update["update_id"] = first_update_id

        metadata: dict[str, Any] = {}
        if len(entries) > 1:
            metadata = {
                "coalesced_parts": len(entries),
                "message_ids": [message.get("message_id") for _, message in entries],
                "update_ids": [uid for uid, _ in entries if uid is not None],
            }
            logger.info(
                "telegram.fragments_coalesced",
                chat_id=key[0],
                parts=len(entries),
                chars=len(merged["text"]),
                wait_ms=round((asyncio.get_running_loop().time() - buffer.started_at) * 1000),
                reason=reason,
                message_ids=metadata["message_ids"],
            )
        try:
            self._callback(update, metadata)
        except Exception:
            logger.exception("telegram.split_text.flush_error", chat_id=key[0])
