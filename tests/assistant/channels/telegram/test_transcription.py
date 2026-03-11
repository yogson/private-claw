"""
Unit tests for VoiceTranscriptionService and TelegramIngress.normalize_async().
"""

import asyncio

import pytest

from assistant.channels.telegram.allowlist import AllowlistGuard
from assistant.channels.telegram.ingestion.interfaces import TranscriptionWorkerInterface
from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.channels.telegram.ingress import _VOICE_MISSING_TRANSCRIPT, TelegramIngress
from assistant.channels.telegram.models import EventType

# --- Helpers ---


def _make_voice_update(
    user_id: int = 123456,
    file_id: str = "voice_abc",
    duration: int = 5,
    message_id: int = 10,
    inline_transcript: str | None = None,
) -> dict:
    msg: dict = {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": user_id},
        "date": 1700000000,
        "voice": {"file_id": file_id, "duration": duration},
    }
    if inline_transcript is not None:
        msg["text"] = inline_transcript
    return {"message": msg}


class _StubWorker(TranscriptionWorkerInterface):
    """Test double that returns a preset result."""

    def __init__(self, result: str | None = "hello world", delay: float = 0.0) -> None:
        self._result = result
        self._delay = delay

    async def transcribe(self, file_id: str, duration_seconds: int) -> str | None:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result


class _RaisingWorker(TranscriptionWorkerInterface):
    """Test double that raises on transcription."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def transcribe(self, file_id: str, duration_seconds: int) -> str | None:
        raise self._exc


def _make_ingress(
    allowed: list[int] | None = None,
    service: VoiceTranscriptionService | None = None,
) -> TelegramIngress:
    return TelegramIngress(
        AllowlistGuard(allowed or [123456]),
        transcription_service=service,
    )


# --- VoiceTranscriptionService tests ---


class TestVoiceTranscriptionService:
    @pytest.mark.asyncio
    async def test_success_returns_transcript_and_no_reason(self) -> None:
        svc = VoiceTranscriptionService(_StubWorker("hey there"), timeout_seconds=5)
        text, reason = await svc.transcribe("fid", 3, "trace1")
        assert text == "hey there"
        assert reason is None

    @pytest.mark.asyncio
    async def test_empty_result_returns_none_and_empty_reason(self) -> None:
        svc = VoiceTranscriptionService(_StubWorker(None), timeout_seconds=5)
        text, reason = await svc.transcribe("fid", 3, "trace1")
        assert text is None
        assert reason == "empty_result"

    @pytest.mark.asyncio
    async def test_timeout_returns_none_and_timeout_reason(self) -> None:
        svc = VoiceTranscriptionService(_StubWorker(delay=0.5), timeout_seconds=1)
        # Use a very short timeout to force a timeout
        svc._timeout = 0.01  # type: ignore[attr-defined]
        text, reason = await svc.transcribe("fid", 3, "trace1")
        assert text is None
        assert reason == "timeout"

    @pytest.mark.asyncio
    async def test_worker_exception_returns_none_and_error_reason(self) -> None:
        svc = VoiceTranscriptionService(
            _RaisingWorker(ValueError("quota exceeded")), timeout_seconds=5
        )
        text, reason = await svc.transcribe("fid", 3, "trace1")
        assert text is None
        assert reason is not None
        assert "ValueError" in reason

    @pytest.mark.asyncio
    async def test_never_raises(self) -> None:
        svc = VoiceTranscriptionService(
            _RaisingWorker(RuntimeError("unexpected")), timeout_seconds=5
        )
        text, reason = await svc.transcribe("fid", 3, "trace1")
        assert text is None
        assert reason is not None


# --- TelegramIngress.normalize_async() tests ---


class TestNormalizeAsync:
    @pytest.mark.asyncio
    async def test_text_update_returns_same_as_sync(self) -> None:
        ingress = _make_ingress()
        update = {
            "message": {
                "message_id": 1,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "text": "hello",
            }
        }
        event = await ingress.normalize_async(update)
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE

    @pytest.mark.asyncio
    async def test_voice_no_service_returns_fallback_text(self) -> None:
        ingress = _make_ingress(service=None)
        event = await ingress.normalize_async(_make_voice_update())
        assert event is not None
        assert event.text == _VOICE_MISSING_TRANSCRIPT
        assert event.voice is not None
        assert event.voice.transcript_text is None

    @pytest.mark.asyncio
    async def test_voice_with_service_enriches_transcript(self) -> None:
        svc = VoiceTranscriptionService(_StubWorker("transcribed!"), timeout_seconds=5)
        ingress = _make_ingress(service=svc)
        event = await ingress.normalize_async(_make_voice_update(file_id="fid_1"))
        assert event is not None
        assert event.event_type == EventType.USER_VOICE_MESSAGE
        assert event.text == "transcribed!"
        assert event.voice is not None
        assert event.voice.transcript_text == "transcribed!"

    @pytest.mark.asyncio
    async def test_voice_with_inline_transcript_skips_service(self) -> None:
        """When Telegram provides inline transcript, MTProto worker is not called."""
        call_count = 0

        class _CountingWorker(TranscriptionWorkerInterface):
            async def transcribe(self, file_id: str, duration_seconds: int) -> str | None:
                nonlocal call_count
                call_count += 1
                return "from worker"

        svc = VoiceTranscriptionService(_CountingWorker(), timeout_seconds=5)
        ingress = _make_ingress(service=svc)
        event = await ingress.normalize_async(
            _make_voice_update(inline_transcript="already transcribed")
        )
        assert event is not None
        assert event.text == "already transcribed"
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_voice_transcription_failure_stores_audit_reason(self) -> None:
        svc = VoiceTranscriptionService(_StubWorker(None), timeout_seconds=5)
        ingress = _make_ingress(service=svc)
        event = await ingress.normalize_async(_make_voice_update())
        assert event is not None
        assert event.metadata.get("audit_transcription_failure") == "empty_result"
        assert event.text == _VOICE_MISSING_TRANSCRIPT

    @pytest.mark.asyncio
    async def test_voice_transcription_timeout_stores_audit_reason(self) -> None:
        svc = VoiceTranscriptionService(_StubWorker(delay=0.5), timeout_seconds=5)
        svc._timeout = 0.01  # type: ignore[attr-defined]
        ingress = _make_ingress(service=svc)
        event = await ingress.normalize_async(_make_voice_update())
        assert event is not None
        assert event.metadata.get("audit_transcription_failure") == "timeout"

    @pytest.mark.asyncio
    async def test_unsupported_update_returns_none(self) -> None:
        ingress = _make_ingress()
        event = await ingress.normalize_async({"edited_message": {}})
        assert event is None


# --- TelegramAdapter.process_update_async() tests ---


class TestAdapterProcessUpdateAsync:
    @pytest.mark.asyncio
    async def test_async_update_text_message(self) -> None:
        from assistant.channels.telegram.adapter import TelegramAdapter
        from assistant.core.config.schemas import TelegramChannelConfig

        config = TelegramChannelConfig(
            enabled=True,
            bot_token="12345:test-token",
            allowlist=[123456],
        )
        adapter = TelegramAdapter(config)
        update = {
            "message": {
                "message_id": 1,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "text": "ping",
            }
        }
        event = await adapter.process_update_async(update)
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE

    @pytest.mark.asyncio
    async def test_async_update_voice_no_transcription_service(self) -> None:
        from assistant.channels.telegram.adapter import TelegramAdapter
        from assistant.core.config.schemas import TelegramChannelConfig

        config = TelegramChannelConfig(
            enabled=True,
            bot_token="12345:test-token",
            allowlist=[123456],
        )
        adapter = TelegramAdapter(config)
        event = await adapter.process_update_async(_make_voice_update())
        assert event is not None
        assert event.event_type == EventType.USER_VOICE_MESSAGE
        # No transcription service configured - fallback text expected
        assert event.text == _VOICE_MISSING_TRANSCRIPT
