"""
Unit tests for TelegramAdapter.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Chat, Message, Update, User

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.allowlist import UnauthorizedUserError
from assistant.channels.telegram.ingestion.interfaces import TranscriptionWorkerInterface
from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.channels.telegram.models import ChannelResponse, EventType, MessageType
from assistant.core.config.schemas import TelegramChannelConfig


def _make_config(allowlist: list[int] | None = None) -> TelegramChannelConfig:
    return TelegramChannelConfig(
        enabled=True,
        bot_token="12345:test-token",
        allowlist=allowlist or [123456],
        webhook_url="https://example.com/telegram/webhook",
    )


def _text_update(user_id: int = 123456, text: str = "hi") -> dict:
    return {
        "message": {
            "message_id": 1,
            "from": {"id": user_id},
            "chat": {"id": user_id},
            "date": 1700000000,
            "text": text,
        }
    }


def _make_response() -> ChannelResponse:
    return ChannelResponse(
        response_id=str(uuid.uuid4()),
        channel="telegram",
        session_id="tg:123456",
        trace_id="trace-x",
        message_type=MessageType.TEXT,
        text="Reply!",
    )


class TestTelegramAdapterIngress:
    def test_process_update_allowed_user(self) -> None:
        adapter = TelegramAdapter(_make_config())
        event = adapter.process_update(_text_update())
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE

    def test_process_update_unauthorized_raises(self) -> None:
        adapter = TelegramAdapter(_make_config(allowlist=[111]))
        with pytest.raises(UnauthorizedUserError):
            adapter.process_update(_text_update(user_id=999))

    def test_process_update_unsupported_returns_none(self) -> None:
        adapter = TelegramAdapter(_make_config())
        assert adapter.process_update({"some_other": {}}) is None

    def test_process_update_unexpected_exception_returns_none(self) -> None:
        adapter = TelegramAdapter(_make_config())
        with patch.object(adapter._ingress, "normalize", side_effect=RuntimeError("boom")):
            result = adapter.process_update(_text_update())
        assert result is None

    def test_process_update_accepts_aiogram_update(self) -> None:
        adapter = TelegramAdapter(_make_config())
        update = Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=1700000000,
                chat=Chat(id=123456, type="private"),
                from_user=User(id=123456, is_bot=False, first_name="User"),
                text="hi",
            ),
        )
        event = adapter.process_update(update)
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE


class TestTranscriptionInjection:
    @pytest.mark.asyncio
    async def test_injected_service_enriches_voice_via_async_path(self) -> None:
        class _Worker(TranscriptionWorkerInterface):
            async def transcribe(self, file_id: str, duration_seconds: int) -> str | None:
                return "injected transcript"

        svc = VoiceTranscriptionService(_Worker(), timeout_seconds=5)
        adapter = TelegramAdapter(_make_config(), transcription_service=svc)

        voice_update = {
            "message": {
                "message_id": 20,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "voice": {"file_id": "voice_x", "duration": 4},
            }
        }
        event = await adapter.process_update_async(voice_update)
        assert event is not None
        assert event.event_type == EventType.USER_VOICE_MESSAGE
        assert event.text == "injected transcript"
        assert event.voice is not None
        assert event.voice.transcript_text == "injected transcript"

    @pytest.mark.asyncio
    async def test_no_service_voice_returns_fallback_text(self) -> None:
        adapter = TelegramAdapter(_make_config())
        voice_update = {
            "message": {
                "message_id": 21,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "voice": {"file_id": "voice_y", "duration": 3},
            }
        }
        event = await adapter.process_update_async(voice_update)
        assert event is not None
        assert event.voice is not None
        assert event.voice.transcript_text is None

    @pytest.mark.asyncio
    async def test_process_update_async_unexpected_exception_returns_none(self) -> None:
        adapter = TelegramAdapter(_make_config())
        with patch.object(
            adapter._ingress,
            "normalize_async",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = await adapter.process_update_async(_text_update())
        assert result is None


class TestTelegramAdapterEgress:
    @pytest.mark.asyncio
    async def test_send_response_delegates_to_egress(self) -> None:
        adapter = TelegramAdapter(_make_config())
        with patch.object(adapter._egress, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            result = await adapter.send_response(_make_response(), chat_id=123456)
        assert result is True
        mock_send.assert_called_once()
