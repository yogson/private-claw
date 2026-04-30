"""
Unit tests for TelegramIngress.
"""

import json

import pytest

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError
from assistant.channels.telegram.ingress import _VOICE_MISSING_TRANSCRIPT, TelegramIngress
from assistant.channels.telegram.ingress_builders import build_web_app_data_event, parse_date
from assistant.channels.telegram.models import EventSource, EventType


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


class TestWebAppDataMessage:
    def _web_app_update(
        self,
        user_id: int = 123456,
        chat_id: int = 123456,
        message_id: int = 20,
        data: str = '{"type":"exercise_results","results":[]}',
        button_text: str = "🃏 Start",
    ) -> dict:
        return {
            "message": {
                "message_id": message_id,
                "from": {"id": user_id},
                "chat": {"id": chat_id},
                "date": 1700000000,
                "web_app_data": {
                    "data": data,
                    "button_text": button_text,
                },
            }
        }

    def test_web_app_data_non_exercise_type_routes_to_text_event(self) -> None:
        """web_app_data with a non-exercise-results type is normalized as USER_TEXT_MESSAGE."""
        ingress = _make_ingress()
        event = ingress.normalize(self._web_app_update(data='{"type":"other_action","value":"x"}'))
        assert event is not None
        assert event.event_type == EventType.USER_TEXT_MESSAGE

    def test_web_app_data_exercise_results_routes_to_exercise_results_event(self) -> None:
        """web_app_data with type=exercise_results is normalized as EXERCISE_RESULTS."""
        ingress = _make_ingress()
        event = ingress.normalize(self._web_app_update())
        assert event is not None
        assert event.event_type == EventType.EXERCISE_RESULTS

    def test_web_app_data_text_contains_payload(self) -> None:
        """The JSON payload from web_app_data.data becomes event.text."""
        ingress = _make_ingress()
        payload = '{"type":"exercise_results","results":[]}'
        event = ingress.normalize(self._web_app_update(data=payload))
        assert event is not None
        assert event.text == payload

    def test_web_app_data_session_and_user(self) -> None:
        """Session ID and user ID are set correctly from chat/from fields."""
        ingress = _make_ingress()
        event = ingress.normalize(self._web_app_update(user_id=123456, chat_id=123456))
        assert event is not None
        assert event.user_id == "123456"
        assert event.session_id == "tg:123456"

    def test_web_app_data_idempotency_key_uses_message_id(self) -> None:
        """Idempotency key includes the Telegram message_id."""
        ingress = _make_ingress()
        event = ingress.normalize(self._web_app_update(message_id=99))
        assert event is not None
        assert "99" in (event.idempotency_key or "")

    def test_web_app_data_unknown_user_raises(self) -> None:
        """Unauthorized user raises UnauthorizedUserError."""
        ingress = _make_ingress(allowed=[111])
        with pytest.raises(UnauthorizedUserError):
            ingress.normalize(self._web_app_update(user_id=999))

    def test_web_app_data_empty_data_gives_none_text(self) -> None:
        """Empty data field results in event.text being None."""
        ingress = _make_ingress()
        event = ingress.normalize(self._web_app_update(data=""))
        assert event is not None
        assert event.text is None


class TestBuildWebAppDataEvent:
    def test_build_web_app_data_event_happy_path(self) -> None:
        """build_web_app_data_event extracts data and sets correct event fields."""
        message = {
            "message_id": 42,
            "from": {"id": 123456},
            "chat": {"id": 123456},
            "date": 1700000000,
            "web_app_data": {
                "data": '{"type":"exercise_results"}',
                "button_text": "Start",
            },
        }
        created_at = parse_date(message["date"])
        event = build_web_app_data_event(
            message,
            user_id=123456,
            session_id="tg:123456",
            event_id="evt-1",
            trace_id="trace-1",
            created_at=created_at,
        )
        assert event.event_type == EventType.EXERCISE_RESULTS
        assert event.source == EventSource.TELEGRAM
        assert event.text == '{"type":"exercise_results"}'
        assert event.session_id == "tg:123456"
        assert event.user_id == "123456"
        assert "42" in event.idempotency_key

    def test_build_web_app_data_event_missing_data_field(self) -> None:
        """When web_app_data.data is absent, event.text is None."""
        message = {
            "message_id": 43,
            "from": {"id": 123456},
            "chat": {"id": 123456},
            "date": 1700000000,
            "web_app_data": {},
        }
        created_at = parse_date(message["date"])
        event = build_web_app_data_event(
            message,
            user_id=123456,
            session_id="tg:123456",
            event_id="evt-2",
            trace_id="trace-2",
            created_at=created_at,
        )
        assert event.text is None

    def test_build_web_app_data_event_metadata_contains_chat_id(self) -> None:
        """Metadata chat_id is populated from message.chat.id."""
        message = {
            "message_id": 44,
            "from": {"id": 123456},
            "chat": {"id": 999888},
            "date": 1700000000,
            "web_app_data": {"data": "hello"},
        }
        created_at = parse_date(message["date"])
        event = build_web_app_data_event(
            message,
            user_id=123456,
            session_id="tg:999888",
            event_id="evt-3",
            trace_id="trace-3",
            created_at=created_at,
        )
        assert event.metadata.get("chat_id") == 999888


class TestBuildWebAppDataEventExerciseResultsDetection:
    def _build_event(self, data: str) -> object:
        message = {
            "message_id": 60,
            "from": {"id": 123456},
            "chat": {"id": 123456},
            "date": 1700000000,
            "web_app_data": {"data": data, "button_text": "Start"},
        }
        created_at = parse_date(message["date"])
        return build_web_app_data_event(
            message, 123456, "tg:123456", "evt-60", "trace-60", created_at
        )

    def test_exercise_results_payload_creates_exercise_results_event(self) -> None:
        """JSON with type=exercise_results produces an EXERCISE_RESULTS event."""
        data = json.dumps(
            {
                "type": "exercise_results",
                "results": [{"word_id": "abc", "rating": 3, "direction": "forward"}],
            }
        )
        event = self._build_event(data)
        assert event.event_type == EventType.EXERCISE_RESULTS

    def test_exercise_results_event_preserves_raw_json_in_text(self) -> None:
        """Raw JSON string is still accessible via event.text."""
        data = json.dumps({"type": "exercise_results", "results": []})
        event = self._build_event(data)
        assert event.text == data

    def test_non_exercise_type_creates_text_event(self) -> None:
        """JSON with a different type stays USER_TEXT_MESSAGE."""
        data = json.dumps({"type": "start_game", "level": 1})
        event = self._build_event(data)
        assert event.event_type == EventType.USER_TEXT_MESSAGE

    def test_invalid_json_creates_text_event(self) -> None:
        """Non-parseable data stays USER_TEXT_MESSAGE."""
        event = self._build_event("this is not { json }")
        assert event.event_type == EventType.USER_TEXT_MESSAGE
