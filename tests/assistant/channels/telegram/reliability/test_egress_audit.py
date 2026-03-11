"""
Integration tests verifying ChannelAuditLogger wiring in TelegramEgress
across all retry paths: success, retry-after, network error, api error,
and terminal failure.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.methods import SendMessage

from assistant.channels.telegram.egress import TelegramEgress, TelegramSendError
from assistant.channels.telegram.models import ChannelResponse, MessageType
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger


def _make_response(trace_id: str = "trace-x") -> ChannelResponse:
    return ChannelResponse(
        response_id="resp-1",
        channel="telegram",
        session_id="tg:456",
        trace_id=trace_id,
        message_type=MessageType.TEXT,
        text="hi",
    )


def _send_method() -> SendMessage:
    return SendMessage(chat_id=456, text="hi")


def _make_egress(max_attempts: int = 3) -> tuple[TelegramEgress, MagicMock]:
    audit = MagicMock(spec=ChannelAuditLogger)
    egress = TelegramEgress(
        bot_token="12345:test-token",
        max_attempts=max_attempts,
        base_delay=0.0,
        audit_logger=audit,
    )
    return egress, audit


class TestEgressAuditOnSuccess:
    @pytest.mark.asyncio
    async def test_success_emits_attempt_and_success(self) -> None:
        egress, audit = _make_egress(max_attempts=1)
        response = _make_response()
        with patch.object(egress._bot, "send_message", new_callable=AsyncMock):
            await egress.send(response, chat_id=456)
        audit.log_egress_attempt.assert_called_once_with(
            chat_id=456, response_id="resp-1", attempt=1, trace_id="trace-x"
        )
        audit.log_egress_success.assert_called_once_with(
            chat_id=456, response_id="resp-1", attempts=1, trace_id="trace-x"
        )
        audit.log_egress_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_on_second_attempt_emits_two_attempts(self) -> None:
        egress, audit = _make_egress(max_attempts=3)
        response = _make_response()
        call_count = 0

        async def flaky(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TelegramNetworkError(_send_method(), "timeout")

        with (
            patch.object(egress._bot, "send_message", side_effect=flaky),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await egress.send(response, chat_id=456)

        assert audit.log_egress_attempt.call_count == 2
        audit.log_egress_success.assert_called_once()
        audit.log_egress_failure.assert_not_called()


class TestEgressAuditOnNetworkError:
    @pytest.mark.asyncio
    async def test_network_error_exhausted_emits_network_error_and_failure(self) -> None:
        egress, audit = _make_egress(max_attempts=2)
        response = _make_response()
        with (
            patch.object(
                egress._bot,
                "send_message",
                new_callable=AsyncMock,
                side_effect=TelegramNetworkError(_send_method(), "refused"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(TelegramSendError),
        ):
            await egress.send(response, chat_id=456)

        assert audit.log_egress_network_error.call_count == 2
        audit.log_egress_failure.assert_called_once()
        failure_kwargs = audit.log_egress_failure.call_args.kwargs
        assert failure_kwargs["chat_id"] == 456
        assert failure_kwargs["attempts"] == 2
        assert failure_kwargs["trace_id"] == "trace-x"


class TestEgressAuditOnApiError:
    @pytest.mark.asyncio
    async def test_api_error_emits_api_error_and_failure(self) -> None:
        egress, audit = _make_egress(max_attempts=3)
        response = _make_response()
        with (
            patch.object(
                egress._bot,
                "send_message",
                new_callable=AsyncMock,
                side_effect=TelegramAPIError(_send_method(), "Forbidden"),
            ),
            pytest.raises(TelegramSendError),
        ):
            await egress.send(response, chat_id=456)

        audit.log_egress_api_error.assert_called_once()
        api_kwargs = audit.log_egress_api_error.call_args.kwargs
        assert api_kwargs["attempt"] == 1
        audit.log_egress_failure.assert_called_once_with(
            chat_id=456,
            response_id="resp-1",
            attempts=1,
            error=api_kwargs["error"],
            trace_id="trace-x",
        )


class TestEgressAuditOnRetryAfter:
    @pytest.mark.asyncio
    async def test_retry_after_emits_retry_after_audit(self) -> None:
        from aiogram.exceptions import TelegramRetryAfter

        egress, audit = _make_egress(max_attempts=2)
        response = _make_response()

        call_count = 0

        async def raise_then_succeed(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                exc = TelegramRetryAfter(_send_method(), "retry after 1", retry_after=1)
                raise exc

        with (
            patch.object(egress._bot, "send_message", side_effect=raise_then_succeed),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await egress.send(response, chat_id=456)

        audit.log_egress_retry_after.assert_called_once()
        ra_kwargs = audit.log_egress_retry_after.call_args.kwargs
        assert ra_kwargs["attempt"] == 1
        assert ra_kwargs["retry_after"] == 1.0
        assert ra_kwargs["trace_id"] == "trace-x"
        audit.log_egress_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_after_exhausted_emits_failure(self) -> None:
        from aiogram.exceptions import TelegramRetryAfter

        egress, audit = _make_egress(max_attempts=2)
        response = _make_response()

        with (
            patch.object(
                egress._bot,
                "send_message",
                new_callable=AsyncMock,
                side_effect=TelegramRetryAfter(_send_method(), "retry after 1", retry_after=1),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(TelegramSendError),
        ):
            await egress.send(response, chat_id=456)

        assert audit.log_egress_retry_after.call_count == 2
        audit.log_egress_failure.assert_called_once()
