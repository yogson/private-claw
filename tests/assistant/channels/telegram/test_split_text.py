"""
Tests for coalescing Telegram-split long texts into one event (issue #101).
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Update
from structlog.testing import capture_logs

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.ingestion import split_text
from assistant.channels.telegram.ingestion.split_text import (
    SPLIT_PART_MIN_UTF16,
    SplitMessageCoalescer,
    utf16_len,
)
from assistant.channels.telegram.models import ChannelResponse, NormalizedEvent
from assistant.channels.telegram.polling import run_polling
from assistant.core.config.schemas import TelegramChannelConfig

USER = 123456
OTHER_USER = 654321
LONG_A = "a" * 4000
LONG_B = "b" * 4000
TAIL = "tail of the paste"


def _update(
    update_id: int,
    text: str,
    *,
    message_id: int | None = None,
    user_id: int = USER,
    chat_id: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id if message_id is None else message_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": user_id if chat_id is None else chat_id, "type": "private"},
            "date": 1700000000,
            "text": text,
            **extra,
        },
    }


class _Collector:
    def __init__(self) -> None:
        self.flushed: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def __call__(self, update: dict[str, Any], metadata: dict[str, Any]) -> None:
        self.flushed.append((update, metadata))

    @property
    def texts(self) -> list[str]:
        return [update["message"]["text"] for update, _ in self.flushed]


# --- SplitMessageCoalescer ---------------------------------------------------


async def test_three_parts_flush_as_one_update_in_message_id_order() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=10)

    # Parts are offered out of order; the flush orders them by message_id.
    assert coalescer.offer(_update(11, LONG_B, message_id=11)) is True
    assert coalescer.offer(_update(10, LONG_A, message_id=10)) is True
    assert sink.flushed == []
    assert coalescer.offer(_update(12, TAIL, message_id=12)) is True  # tail flushes at once

    assert sink.texts == [f"{LONG_A}\n{LONG_B}\n{TAIL}"]
    update, metadata = sink.flushed[0]
    assert update["update_id"] == 10
    assert update["message"]["message_id"] == 10
    assert metadata == {
        "coalesced_parts": 3,
        "message_ids": [10, 11, 12],
        "update_ids": [10, 11, 12],
    }
    assert coalescer.pending == 0


async def test_join_does_not_add_newline_when_cut_kept_whitespace() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=10)
    coalescer.offer(_update(1, LONG_A + "\n"))
    coalescer.offer(_update(2, " " + TAIL))
    assert sink.texts == [f"{LONG_A}\n {TAIL}"]


async def test_short_message_is_not_buffered() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=10)
    assert coalescer.offer(_update(1, "hello")) is False
    assert coalescer.pending == 0
    assert sink.flushed == []


async def test_threshold_counts_utf16_units() -> None:
    emoji_text = "\U0001f600" * (SPLIT_PART_MIN_UTF16 // 2)  # 1024 code points, 2048 units
    assert len(emoji_text) < SPLIT_PART_MIN_UTF16 <= utf16_len(emoji_text)
    coalescer = SplitMessageCoalescer(_Collector(), window_seconds=10)
    assert coalescer.offer(_update(1, emoji_text)) is True
    assert coalescer.offer(_update(2, "x" * (SPLIT_PART_MIN_UTF16 - 1), user_id=OTHER_USER)) is (
        False
    )
    coalescer.flush_all()


@pytest.mark.parametrize(
    "update",
    [
        _update(2, "/stop"),
        _update(2, "/new " + LONG_B),
        _update(
            2, LONG_B, forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "x"}
        ),
        {
            "update_id": 2,
            "message": {
                "message_id": 2,
                "from": {"id": USER, "is_bot": False, "first_name": "Test"},
                "chat": {"id": USER, "type": "private"},
                "date": 1700000000,
                "caption": "a photo",
                "photo": [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}],
            },
        },
    ],
    ids=["stop", "long_command", "forward", "photo"],
)
async def test_other_message_flushes_buffer_first_and_is_not_merged(update: dict[str, Any]) -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=10)
    assert coalescer.offer(_update(1, LONG_A)) is True

    # The caller dispatches it itself; the pending text is flushed before, unmerged.
    assert coalescer.offer(update) is False
    assert sink.texts == [LONG_A]
    assert sink.flushed[0][1] == {}  # a single part carries no coalescing metadata
    assert coalescer.pending == 0


async def test_long_command_never_opens_a_buffer() -> None:
    coalescer = SplitMessageCoalescer(_Collector(), window_seconds=10)
    assert coalescer.offer(_update(1, "/stop " + LONG_A)) is False
    assert coalescer.pending == 0


async def test_callback_query_passes_through_without_flushing() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=10)
    coalescer.offer(_update(1, LONG_A))
    callback = {"update_id": 2, "callback_query": {"id": "cq", "from": {"id": USER}, "data": "x"}}
    assert coalescer.offer(callback) is False
    assert sink.flushed == []
    coalescer.flush_all()


async def test_different_chats_and_users_do_not_merge() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=10)
    coalescer.offer(_update(1, LONG_A))
    coalescer.offer(_update(2, LONG_B, user_id=OTHER_USER))
    # Same user, different (group) chat.
    coalescer.offer(_update(3, LONG_B, chat_id=-100))
    assert coalescer.pending == 3

    coalescer.offer(_update(4, TAIL, user_id=OTHER_USER))
    assert sink.texts == [f"{LONG_B}\n{TAIL}"]
    assert sink.flushed[0][1]["message_ids"] == [2, 4]

    coalescer.flush_all()
    assert sorted(sink.texts[1:]) == [LONG_A, LONG_B]


async def test_part_after_window_is_a_separate_turn() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=0.05)
    coalescer.offer(_update(1, LONG_A))
    await asyncio.sleep(0.15)
    assert sink.texts == [LONG_A]  # flushed alone when the window expired
    assert coalescer.pending == 0

    # A later short message is not merged into the already flushed text.
    assert coalescer.offer(_update(2, TAIL)) is False
    assert sink.texts == [LONG_A]


async def test_long_part_restarts_the_window() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=0.1)
    coalescer.offer(_update(1, LONG_A))
    await asyncio.sleep(0.06)
    coalescer.offer(_update(2, LONG_B))
    await asyncio.sleep(0.06)  # 0.12 s after part 1, but only 0.06 s after part 2
    assert sink.flushed == []
    await asyncio.sleep(0.1)
    assert sink.texts == [f"{LONG_A}\n{LONG_B}"]


async def test_flush_all_cancels_timers_and_flushes_buffers() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=0.05)
    coalescer.offer(_update(1, LONG_A))
    coalescer.offer(_update(2, LONG_B))
    coalescer.offer(_update(3, LONG_B, user_id=OTHER_USER))

    with capture_logs() as logs:
        coalescer.flush_all()
    assert coalescer.pending == 0
    assert sorted(sink.texts) == sorted([f"{LONG_A}\n{LONG_B}", LONG_B])
    assert [e["reason"] for e in logs if e["event"] == "telegram.fragments_coalesced"] == [
        "shutdown"
    ]

    await asyncio.sleep(0.1)  # the cancelled timers must not flush again
    assert len(sink.flushed) == 2
    coalescer.flush_all()  # idempotent
    assert len(sink.flushed) == 2


async def test_duplicate_part_is_dropped_and_key_is_from_first_part() -> None:
    sink = _Collector()
    coalescer = SplitMessageCoalescer(sink, window_seconds=10)
    coalescer.offer(_update(1, LONG_A, message_id=50))
    assert coalescer.offer(_update(1, LONG_A, message_id=50)) is True  # redelivered
    coalescer.offer(_update(2, TAIL, message_id=51))

    assert sink.texts == [f"{LONG_A}\n{TAIL}"]
    assert sink.flushed[0][1]["message_ids"] == [50, 51]

    adapter = TelegramAdapter(_config())
    update, metadata = sink.flushed[0]
    event = adapter.process_update(update)
    assert event is not None
    # The merged event is keyed by its first Telegram message, like a plain message.
    assert event.idempotency_key == "telegram:50"
    assert event.metadata["message_id"] == 50
    assert event.metadata["update_id"] == 1


async def test_flush_callback_error_is_logged_and_buffer_released() -> None:
    def _boom(update: dict[str, Any], metadata: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    coalescer = SplitMessageCoalescer(_boom, window_seconds=10)
    coalescer.offer(_update(1, LONG_A))
    with capture_logs() as logs:
        coalescer.offer(_update(2, TAIL))
    assert coalescer.pending == 0
    assert any(e["event"] == "telegram.split_text.flush_error" for e in logs)


# --- run_polling integration -------------------------------------------------


def _config() -> TelegramChannelConfig:
    return TelegramChannelConfig(
        enabled=True,
        bot_token="12345:test-token",
        allowlist=[USER, OTHER_USER],
        poll_timeout_seconds=1,
        startup_drop_pending_updates=False,
    )


async def _run(
    batches: list[list[dict[str, Any]]],
    *,
    stop_after_events: int | None,
    stop_after_polls: int | None = None,
    timeout: float = 5,
    delays: list[float] | None = None,
) -> tuple[list[NormalizedEvent], list[SplitMessageCoalescer]]:
    """Feed update batches through run_polling; return handled events and the coalescer."""
    config = _config()
    adapter = TelegramAdapter(config)
    stop = asyncio.Event()
    queue = [[Update.model_validate(u) for u in batch] for batch in batches]
    pending_delays = list(delays or [])
    polls = 0

    async def _get_updates(**_: object) -> list[Update]:
        nonlocal polls
        polls += 1
        if stop_after_polls is not None and polls > stop_after_polls:
            stop.set()
        if queue:
            if pending_delays:
                await asyncio.sleep(pending_delays.pop(0))
            return queue.pop(0)
        await asyncio.sleep(0.01)
        return []

    events: list[NormalizedEvent] = []

    async def _handler(event: NormalizedEvent) -> ChannelResponse | None:
        events.append(event)
        if stop_after_events is not None and len(events) >= stop_after_events:
            stop.set()
        return None

    instances: list[SplitMessageCoalescer] = []

    def _factory(*args: Any, **kwargs: Any) -> SplitMessageCoalescer:
        instances.append(SplitMessageCoalescer(*args, **kwargs))
        return instances[-1]

    with (
        patch("assistant.channels.telegram.polling.Bot") as mock_bot_cls,
        patch("assistant.channels.telegram.polling.SplitMessageCoalescer", _factory),
    ):
        mock_bot = MagicMock()
        mock_bot.get_updates = AsyncMock(side_effect=_get_updates)
        mock_bot.delete_webhook = AsyncMock()
        mock_bot.set_my_commands = AsyncMock()
        mock_bot.set_chat_menu_button = AsyncMock()
        mock_bot.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        await asyncio.wait_for(run_polling(adapter, config, _handler, stop_event=stop), timeout)
    return events, instances


async def test_run_polling_turns_split_paste_into_one_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Part 1 arrives alone, parts 2-3 one poll later (as in production): one event."""
    monkeypatch.setattr(split_text, "SPLIT_WINDOW_SECONDS", 10.0)
    events, _ = await _run(
        [[_update(700, LONG_A)], [_update(701, LONG_B), _update(702, TAIL)]],
        stop_after_events=None,
        stop_after_polls=4,
    )

    assert len(events) == 1
    event = events[0]
    assert event.text == f"{LONG_A}\n{LONG_B}\n{TAIL}"
    assert event.idempotency_key == "telegram:700"
    assert event.metadata["update_id"] == 700
    assert event.metadata["update_ids"] == [700, 701, 702]
    assert event.metadata["message_ids"] == [700, 701, 702]
    assert event.metadata["coalesced_parts"] == 3


# Real incident shapes (lengths and the ~2.15 s gap after part 1), replayed with all
# times scaled down by 10 (window 3.5 s -> 0.35 s, gap 2.15 s -> 0.215 s).
_SCALE = 10


def _scale_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        split_text, "SPLIT_WINDOW_SECONDS", split_text.SPLIT_WINDOW_SECONDS / _SCALE
    )


def test_constants_cover_observed_incidents() -> None:
    assert split_text.SPLIT_PART_MIN_UTF16 <= 945
    assert split_text.SPLIT_WINDOW_SECONDS > 2.15


async def test_run_polling_incident_tg1_is_one_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """First part ~1410 chars, then ~3450 and ~3110 chars 2.15 s later: one event."""
    _scale_window(monkeypatch)
    parts = ["a" * 1410, "b" * 3450, "c" * 3110]
    events, _ = await _run(
        [[_update(1, parts[0])], [_update(2, parts[1]), _update(3, parts[2])]],
        stop_after_events=1,
        delays=[0, 2.15 / _SCALE],
    )
    assert [e.text for e in events] == ["\n".join(parts)]
    assert events[0].metadata["message_ids"] == [1, 2, 3]


async def test_run_polling_incident_tg2_path_part_is_one_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """~945, ~4050, ~4070 chars, then ~2540 chars starting with a /path: one event."""
    _scale_window(monkeypatch)
    parts = ["a" * 945, "b" * 4050, "c" * 4070, "/docker-entrypoint.sh: " + "d" * 2517]
    events, _ = await _run(
        [
            [_update(1, parts[0])],
            [_update(2, parts[1]), _update(3, parts[2]), _update(4, parts[3])],
        ],
        stop_after_events=1,
        delays=[0, 2.14 / _SCALE],
    )
    assert [e.text for e in events] == ["\n".join(parts)]
    assert events[0].metadata["message_ids"] == [1, 2, 3, 4]


async def test_run_polling_real_command_flushes_text_then_runs_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real command during an open buffer: the text goes first, the command alone."""
    _scale_window(monkeypatch)
    events, _ = await _run(
        [[_update(1, LONG_A)], [_update(2, "/stop")]],
        stop_after_events=2,
        delays=[0, 0.05],
        timeout=1,
    )
    assert [e.text for e in events] == [LONG_A, "/stop"]


async def test_run_polling_does_not_delay_short_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a 10 s window, short messages and commands are handled well within 1 s."""
    monkeypatch.setattr(split_text, "SPLIT_WINDOW_SECONDS", 10.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    events, _ = await _run(
        [[_update(1, "hello"), _update(2, "/stop"), _update(3, "x" * 511, user_id=OTHER_USER)]],
        stop_after_events=3,
        timeout=1,
    )
    assert loop.time() - started < 1
    assert [e.text for e in events] == ["hello", "/stop", "x" * 511]
    assert all("coalesced_parts" not in e.metadata for e in events)


async def test_run_polling_stop_command_flushes_pending_part_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/stop is not held: the buffered part is dispatched before it, then /stop runs."""
    monkeypatch.setattr(split_text, "SPLIT_WINDOW_SECONDS", 10.0)
    events, _ = await _run(
        [[_update(1, LONG_A), _update(2, "/stop")]], stop_after_events=2, timeout=1
    )
    assert [e.text for e in events] == [LONG_A, "/stop"]


async def test_run_polling_shutdown_releases_buffer_and_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping polling with an open buffer leaves no buffer, timer or task behind."""
    monkeypatch.setattr(split_text, "SPLIT_WINDOW_SECONDS", 10.0)
    events, instances = await _run(
        [[_update(1, LONG_A)]], stop_after_events=None, stop_after_polls=3
    )

    assert events == []  # never reached the handler: in-flight work is cancelled on shutdown
    assert len(instances) == 1 and instances[0].pending == 0
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not current and not t.done()] == []


async def test_run_polling_cancellation_releases_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the polling task (the other shutdown path) also flushes the buffer."""
    monkeypatch.setattr(split_text, "SPLIT_WINDOW_SECONDS", 10.0)
    buffered = asyncio.Event()
    instances: list[SplitMessageCoalescer] = []

    def _factory(*args: Any, **kwargs: Any) -> SplitMessageCoalescer:
        instances.append(SplitMessageCoalescer(*args, **kwargs))
        return instances[-1]

    sent = False

    async def _get_updates(**_: object) -> list[Update]:
        nonlocal sent
        if not sent:
            sent = True
            return [Update.model_validate(_update(1, LONG_A))]
        buffered.set()
        await asyncio.sleep(10)
        return []

    config = _config()
    with (
        patch("assistant.channels.telegram.polling.Bot") as mock_bot_cls,
        patch("assistant.channels.telegram.polling.SplitMessageCoalescer", _factory),
    ):
        mock_bot = MagicMock()
        mock_bot.get_updates = AsyncMock(side_effect=_get_updates)
        mock_bot.delete_webhook = AsyncMock()
        mock_bot.set_my_commands = AsyncMock()
        mock_bot.set_chat_menu_button = AsyncMock()
        mock_bot.session.close = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        task = asyncio.create_task(
            run_polling(TelegramAdapter(config), config, AsyncMock(), stop_event=asyncio.Event())
        )
        await asyncio.wait_for(buffered.wait(), 1)
        assert instances[0].pending == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert instances[0].pending == 0
