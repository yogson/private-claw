"""
Unit tests for TelegramIngress.
"""

import pytest

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError
from assistant.channels.telegram.ingress import _VOICE_MISSING_TRANSCRIPT, TelegramIngress
from assistant.channels.telegram.models import EventType


def _make_ingress(allowed: list[int] | None = None) -> TelegramIngress:
    return TelegramIngress(AllowlistGuard(allowed or [123456]))


def _text_update(
    user_id: int = 123456,
    text: str = "Hello",
    message_id: int = 1,
    chat_id: int = 123456,
) -> dict:
    return {
        "message": {
            "message_id": message_id,
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "date": 1700000000,
            "text": text,
        }
    }


def _callback_update(
    user_id: int = 123456,
    callback_id: str = "cq1",
    data: str = "action:resume",
    message_id: int = 10,
    chat_id: int = 123456,
) -> dict:
    return {
        "callback_query": {
            "id": callback_id,
            "from": {"id": user_id},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id},
            },
        }
    }


class TestTextMessage:
    def test_text_message_normalizes_correctly(self) -> None:
        ingress = _make_ingress()
        event = ingress.normalize(_text_update())
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE
        assert event.text == "Hello"
        assert event.source == "telegram"
        assert event.user_id == "123456"
        assert event.session_id == "tg:123456"

    def test_idempotency_key_includes_message_id(self) -> None:
        ingress = _make_ingress()
        event = ingress.normalize(_text_update(message_id=42))
        assert event is not None
        assert "42" in (event.idempotency_key or "")

    def test_unknown_user_raises_unauthorized(self) -> None:
        ingress = _make_ingress(allowed=[111])
        with pytest.raises(UnauthorizedUserError):
            ingress.normalize(_text_update(user_id=999))

    def test_missing_from_field_returns_none(self) -> None:
        ingress = _make_ingress()
        update = {
            "message": {
                "message_id": 1,
                "chat": {"id": 1},
                "date": 1700000000,
                "text": "hi",
            }
        }
        assert ingress.normalize(update) is None

    def test_unsupported_update_returns_none(self) -> None:
        ingress = _make_ingress()
        assert ingress.normalize({"edited_message": {}}) is None


class TestCallbackQuery:
    def test_callback_query_normalizes_correctly(self) -> None:
        ingress = _make_ingress()
        event = ingress.normalize(_callback_update())
        assert event is not None
        assert event.event_type == EventType.USER_CALLBACK_QUERY
        assert event.callback_query is not None
        assert event.callback_query.callback_data == "action:resume"
        assert event.callback_query.callback_id == "cq1"

    def test_callback_query_unknown_user_raises(self) -> None:
        ingress = _make_ingress(allowed=[111])
        with pytest.raises(UnauthorizedUserError):
            ingress.normalize(_callback_update(user_id=999))

    def test_callback_idempotency_key_uses_cq_id(self) -> None:
        ingress = _make_ingress()
        event = ingress.normalize(_callback_update(callback_id="cqABC"))
        assert event is not None
        assert "cqABC" in (event.idempotency_key or "")


class TestVoiceMessage:
    def test_voice_without_transcript_sets_fallback_text(self) -> None:
        ingress = _make_ingress()
        update = {
            "message": {
                "message_id": 5,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "voice": {"file_id": "voice_abc", "duration": 10},
            }
        }
        event = ingress.normalize(update)
        assert event is not None
        assert event.event_type == EventType.USER_VOICE_MESSAGE
        assert event.text == _VOICE_MISSING_TRANSCRIPT
        assert event.voice is not None
        assert event.voice.file_id == "voice_abc"

    def test_voice_with_transcript_uses_it(self) -> None:
        ingress = _make_ingress()
        update = {
            "message": {
                "message_id": 6,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "text": "transcribed text",
                "voice": {"file_id": "voice_xyz", "duration": 5},
            }
        }
        event = ingress.normalize(update)
        assert event is not None
        assert event.text == "transcribed text"
        assert event.voice is not None
        assert event.voice.transcript_text == "transcribed text"


class TestAttachmentMessage:
    def test_document_normalizes_correctly(self) -> None:
        ingress = _make_ingress()
        update = {
            "message": {
                "message_id": 7,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "document": {
                    "file_id": "doc_1",
                    "mime_type": "application/pdf",
                    "file_size": 12345,
                },
                "caption": "My doc",
            }
        }
        event = ingress.normalize(update)
        assert event is not None
        assert event.event_type == EventType.USER_ATTACHMENT_MESSAGE
        assert event.attachment is not None
        assert event.attachment.file_id == "doc_1"
        assert event.attachment.mime_type == "application/pdf"
        assert event.text == "My doc"

    def test_photo_picks_largest(self) -> None:
        ingress = _make_ingress()
        update = {
            "message": {
                "message_id": 8,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "photo": [
                    {"file_id": "small", "file_size": 100},
                    {"file_id": "large", "file_size": 9000},
                ],
            }
        }
        event = ingress.normalize(update)
        assert event is not None
        assert event.attachment is not None
        assert event.attachment.file_id == "large"

    def test_document_infers_mime_from_filename_when_octet_stream(self) -> None:
        ingress = _make_ingress()
        update = {
            "message": {
                "message_id": 9,
                "from": {"id": 123456},
                "chat": {"id": 123456},
                "date": 1700000000,
                "document": {
                    "file_id": "doc_md_1",
                    "mime_type": "application/octet-stream",
                    "file_size": 7777,
                    "file_name": "architecture_improvements_exercise.md",
                },
                "caption": "Check this out!",
            }
        }
        event = ingress.normalize(update)
        assert event is not None
        assert event.attachment is not None
        assert event.attachment.file_id == "doc_md_1"
        assert event.attachment.file_name == "architecture_improvements_exercise.md"
        assert event.attachment.mime_type == "text/markdown"
