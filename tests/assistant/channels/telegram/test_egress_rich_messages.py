"""
Unit tests for TelegramEgress rich message delivery (sendRichMessage) and its fallbacks.
"""

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendRichMessage
from aiogram.types import InlineKeyboardMarkup, InputRichMessage
from structlog.testing import capture_logs

from assistant.channels.telegram import egress as egress_module
from assistant.channels.telegram.egress import TelegramEgress, TelegramSendError
from assistant.channels.telegram.models import ActionButton, ChannelResponse, MessageType
from assistant.core.config.schemas import TelegramRichMessagesMode

_TABLE_REPLY = (
    "Here are the results:\n\n"
    "| Name | Score |\n"
    "| :--- | ---: |\n"
    "| **Ann** | 1 |\n"
    "| [Bob](https://example.com) | 2 |\n\n"
    "That's all."
)


@pytest.fixture(autouse=True)
def _reset_rich_latch(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(egress_module._RICH_LATCH, "unsupported", False)
    yield


def _response(text: str, **update: Any) -> ChannelResponse:
    return ChannelResponse(
        response_id=str(uuid.uuid4()),
        channel="telegram",
        session_id="tg:123",
        trace_id="trace-rich",
        message_type=MessageType.TEXT,
        text=text,
    ).model_copy(update=update)


def _interactive(text: str) -> ChannelResponse:
    return _response(
        text,
        message_type=MessageType.INTERACTIVE,
        actions=[ActionButton(label="OK", callback_id="ok", callback_data="ok")],
    )


def _egress(
    mode: TelegramRichMessagesMode = TelegramRichMessagesMode.TABLES_ONLY,
) -> TelegramEgress:
    return TelegramEgress(bot_token="12345:test-token", max_attempts=1, rich_messages=mode)


def _message(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id)


def _rich_method() -> SendRichMessage:
    return SendRichMessage(chat_id=123, rich_message=InputRichMessage(markdown="x"))


def _patch_bot(egress: TelegramEgress) -> tuple[Any, Any]:
    return (
        patch.object(egress._bot, "send_rich_message", new_callable=AsyncMock),
        patch.object(egress._bot, "send_message", new_callable=AsyncMock),
    )


class TestRichMessageSend:
    @pytest.mark.asyncio
    async def test_table_reply_is_sent_as_rich_markdown(self) -> None:
        egress = _egress()
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            mock_rich.return_value = _message(42)
            ids = await egress.send_message_ids(_response(_TABLE_REPLY), chat_id=123)

        assert ids == [42]
        mock_send.assert_not_called()
        mock_rich.assert_called_once()
        kwargs = mock_rich.call_args.kwargs
        assert kwargs["chat_id"] == 123
        rich_message = kwargs["rich_message"]
        assert isinstance(rich_message, InputRichMessage)
        assert rich_message.markdown == _TABLE_REPLY
        assert rich_message.html is None and rich_message.blocks is None
        assert "text" not in kwargs and "entities" not in kwargs and "parse_mode" not in kwargs

    @pytest.mark.asyncio
    async def test_reply_parameters_and_reply_markup_are_passed_through(self) -> None:
        egress = _egress()
        response = _interactive(_TABLE_REPLY).model_copy(update={"reply_to_message_id": 77})
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch:
            mock_rich.return_value = _message(43)
            await egress.send(response, chat_id=123)

        kwargs = mock_rich.call_args.kwargs
        assert kwargs["reply_parameters"].message_id == 77
        assert kwargs["reply_parameters"].allow_sending_without_reply is False
        markup = kwargs["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup)
        assert markup.inline_keyboard[0][0].callback_data == "ok"

    @pytest.mark.asyncio
    async def test_non_table_reply_in_tables_only_mode_uses_entity_path(self) -> None:
        egress = _egress()
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            mock_send.return_value = _message(5)
            ids = await egress.send_message_ids(_response("Just **bold** text."), chat_id=123)

        assert ids == [5]
        mock_rich.assert_not_called()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["text"] == "Just bold text."
        assert kwargs["entities"]

    @pytest.mark.asyncio
    async def test_table_inside_fenced_code_block_uses_entity_path(self) -> None:
        egress = _egress()
        text = "Syntax:\n\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```"
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            await egress.send(_response(text), chat_id=123)

        mock_rich.assert_not_called()
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_mode_sends_non_table_reply_as_rich(self) -> None:
        egress = _egress(TelegramRichMessagesMode.ALL)
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            await egress.send(_response("Just **bold** text."), chat_id=123)

        mock_send.assert_not_called()
        assert mock_rich.call_args.kwargs["rich_message"].markdown == "Just **bold** text."

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", [_TABLE_REPLY, "Just **bold** text."])
    async def test_off_mode_never_calls_rich(self, text: str) -> None:
        egress = _egress(TelegramRichMessagesMode.OFF)
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            await egress.send(_response(text), chat_id=123)

        mock_rich.assert_not_called()
        mock_send.assert_called()

    @pytest.mark.asyncio
    async def test_plain_text_and_parse_mode_responses_skip_rich(self) -> None:
        egress = _egress(TelegramRichMessagesMode.ALL)
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            await egress.send(_response(_TABLE_REPLY, plain_text=True), chat_id=123)
            await egress.send(_response(_TABLE_REPLY, parse_mode="HTML"), chat_id=123)

        mock_rich.assert_not_called()
        assert mock_send.call_count == 2


class TestRichMessageFallback:
    @pytest.mark.asyncio
    async def test_bad_request_falls_back_to_entity_path(self) -> None:
        egress = _egress()
        response = _interactive(_TABLE_REPLY).model_copy(update={"reply_to_message_id": 9})
        error = TelegramBadRequest(_rich_method(), "Bad Request: can't parse rich message")
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send, capture_logs() as logs:
            mock_rich.side_effect = error
            mock_send.return_value = _message(11)
            ids = await egress.send_message_ids(response, chat_id=123)

        assert ids == [11]
        mock_rich.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert "Ann" in kwargs["text"] and "|" in kwargs["text"]
        assert kwargs["entities"]
        assert kwargs["reply_parameters"].message_id == 9
        assert kwargs["reply_markup"] is not None
        events = [entry["event"] for entry in logs]
        assert "telegram.egress.rich_fallback" in events
        assert "telegram.egress.rich_messages_unsupported" not in events
        assert egress_module._RICH_LATCH.unsupported is False

        # A plain BadRequest does not disable the rich path for later replies.
        with rich_patch as mock_rich, plain_patch:
            await egress.send(_response(_TABLE_REPLY), chat_id=123)
        mock_rich.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            TelegramNotFound(_rich_method(), "Not Found"),
            TelegramBadRequest(_rich_method(), "Bad Request: method not found"),
        ],
    )
    async def test_unsupported_method_sets_latch_and_later_sends_skip_rich(
        self, error: Exception
    ) -> None:
        egress = _egress()
        other_egress = _egress(TelegramRichMessagesMode.ALL)
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send, capture_logs() as logs:
            mock_rich.side_effect = error
            mock_send.return_value = _message(12)
            assert await egress.send_message_ids(_response(_TABLE_REPLY), chat_id=123) == [12]
            await egress.send(_response(_TABLE_REPLY), chat_id=123)

        assert egress_module._RICH_LATCH.unsupported is True
        mock_rich.assert_called_once()
        assert mock_send.call_count == 2
        events = [entry["event"] for entry in logs]
        assert events.count("telegram.egress.rich_messages_unsupported") == 1

        # The latch is process-wide: other egress instances skip the rich path too.
        other_rich, other_plain = _patch_bot(other_egress)
        with other_rich as mock_other_rich, other_plain as mock_other_send:
            await other_egress.send(_response("Hi"), chat_id=123)
        mock_other_rich.assert_not_called()
        mock_other_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_rich_failure_then_parse_error_ends_in_plain_text(self) -> None:
        egress = _egress()
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            mock_rich.side_effect = TelegramBadRequest(_rich_method(), "Bad Request: nope")
            mock_send.side_effect = [
                TelegramBadRequest(_rich_method(), "Bad Request: can't parse entities"),
                _message(13),
            ]
            ids = await egress.send_message_ids(_response(_TABLE_REPLY), chat_id=123)

        assert ids == [13]
        last = mock_send.call_args.kwargs
        assert "entities" not in last and "parse_mode" not in last

    @pytest.mark.asyncio
    async def test_failure_after_first_chunk_resends_only_the_rest(self) -> None:
        egress = _egress()
        first = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        second = "Second part"
        response = _response(f"{first}\n\n{second}", reply_to_message_id=3)
        rich_patch, plain_patch = _patch_bot(egress)
        with (
            patch.object(egress_module, "split_rich_markdown", return_value=[first, second]),
            rich_patch as mock_rich,
            plain_patch as mock_send,
        ):
            mock_rich.side_effect = [
                _message(1),
                TelegramBadRequest(_rich_method(), "Bad Request: nope"),
            ]
            mock_send.return_value = _message(2)
            ids = await egress.send_message_ids(response, chat_id=123)

        assert ids == [1, 2]
        assert mock_rich.call_args_list[0].kwargs["reply_parameters"].message_id == 3
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["text"] == "Second part"
        assert "reply_parameters" not in kwargs

    @pytest.mark.asyncio
    async def test_failure_after_first_chunk_puts_reply_markup_on_last_sent_message(self) -> None:
        egress = _egress()
        first = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        response = _interactive(f"{first}\n\nSecond part")
        rich_patch, plain_patch = _patch_bot(egress)
        with (
            patch.object(egress_module, "split_rich_markdown", return_value=[first, "Second part"]),
            rich_patch as mock_rich,
            plain_patch as mock_send,
        ):
            mock_rich.side_effect = [
                _message(1),
                TelegramBadRequest(_rich_method(), "Bad Request: nope"),
            ]
            mock_send.return_value = _message(2)
            await egress.send_message_ids(response, chat_id=123)

        assert mock_rich.call_args_list[0].kwargs["reply_markup"] is None
        assert mock_send.call_args.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            TelegramRetryAfter(_rich_method(), "Too Many Requests", 0),
            TelegramNetworkError(_rich_method(), "network down"),
        ],
    )
    async def test_flood_control_and_network_errors_retry_on_rich_path(
        self, error: Exception
    ) -> None:
        egress = TelegramEgress(
            bot_token="12345:test-token",
            max_attempts=2,
            base_delay=0,
            rich_messages=TelegramRichMessagesMode.TABLES_ONLY,
        )
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            mock_rich.side_effect = [error, _message(5)]
            assert await egress.send_message_ids(_response(_TABLE_REPLY), chat_id=123) == [5]

        assert mock_rich.call_count == 2
        mock_send.assert_not_called()
        assert egress_module._RICH_LATCH.unsupported is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            TelegramForbiddenError(_rich_method(), "Forbidden: bot was blocked by the user"),
            TelegramBadRequest(_rich_method(), "Bad Request: message to be replied not found"),
            TelegramServerError(_rich_method(), "Internal Server Error"),
        ],
    )
    async def test_errors_failing_on_entity_path_too_skip_the_fallback(
        self, error: Exception
    ) -> None:
        audit = MagicMock()
        egress = TelegramEgress(
            bot_token="12345:test-token",
            max_attempts=1,
            rich_messages=TelegramRichMessagesMode.TABLES_ONLY,
            audit_logger=audit,
        )
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send, capture_logs() as logs:
            mock_rich.side_effect = error
            with pytest.raises(TelegramSendError):
                await egress.send_message_ids(_response(_TABLE_REPLY), chat_id=123)

        mock_send.assert_not_called()
        assert "telegram.egress.rich_fallback" not in [entry["event"] for entry in logs]
        audit.log_egress_api_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_rich_failure_is_reported_to_audit_logger(self) -> None:
        audit = MagicMock()
        egress = TelegramEgress(
            bot_token="12345:test-token",
            max_attempts=1,
            rich_messages=TelegramRichMessagesMode.TABLES_ONLY,
            audit_logger=audit,
        )
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            mock_rich.side_effect = TelegramBadRequest(_rich_method(), "Bad Request: nope")
            mock_send.return_value = _message(7)
            await egress.send_message_ids(_response(_TABLE_REPLY), chat_id=123)

        audit.log_egress_api_error.assert_called_once()
        assert "nope" in audit.log_egress_api_error.call_args.kwargs["error"]


class TestRichMessageSplitting:
    @pytest.mark.asyncio
    async def test_oversized_table_is_split_with_header_repeated(self) -> None:
        egress = _egress()
        header = "| id | description |\n| --- | --- |"
        rows = [f"| {i} | {'x' * 200} |" for i in range(400)]
        text = "Big table:\n\n" + header + "\n" + "\n".join(rows) + "\n\nEnd."
        response = _interactive(text).model_copy(update={"reply_to_message_id": 8})
        rich_patch, plain_patch = _patch_bot(egress)
        with rich_patch as mock_rich, plain_patch as mock_send:
            mock_rich.side_effect = [_message(i) for i in range(100, 110)]
            ids = await egress.send_message_ids(response, chat_id=123)

        mock_send.assert_not_called()
        calls = mock_rich.call_args_list
        assert len(calls) > 1
        assert ids == list(range(100, 100 + len(calls)))
        sent_rows: list[str] = []
        for index, call in enumerate(calls):
            markdown = call.kwargs["rich_message"].markdown
            assert len(markdown.encode()) <= 32768
            assert header in markdown
            sent_rows += [
                line for line in markdown.split("\n") if line.startswith("| ") and "x" in line
            ]
            assert ("reply_parameters" in call.kwargs) == (index == 0)
            assert (call.kwargs["reply_markup"] is not None) == (index == len(calls) - 1)
        assert sent_rows == rows
        assert calls[0].kwargs["rich_message"].markdown.startswith("Big table:")
        assert calls[-1].kwargs["rich_message"].markdown.endswith("End.")
