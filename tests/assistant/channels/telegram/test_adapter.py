"""
Unit tests for TelegramAdapter.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Chat, Message, Update, User

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.allowlist import UnauthorizedUserError
from assistant.channels.telegram.ingestion.interfaces import TranscriptionWorkerInterface
from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.channels.telegram.models import (
    CallbackQueryMeta,
    ChannelResponse,
    EventType,
    MessageType,
    NormalizedEvent,
)
from assistant.core.config.schemas import TelegramChannelConfig
from assistant.core.events.models import EventSource


def _make_config(allowlist: list[int] | None = None) -> TelegramChannelConfig:
    return TelegramChannelConfig(
        enabled=True,
        bot_token="12345:test-token",
        allowlist=allowlist or [123456],
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


def test_is_usage_request_returns_true_for_usage_command() -> None:
    """Verifies is_usage_request returns True when event text is /usage."""
    adapter = TelegramAdapter(_make_config())
    event = NormalizedEvent(
        event_id="ev-usage",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-usage",
        text="/usage",
        metadata={"chat_id": 123},
    )
    assert adapter.is_usage_request(event) is True


def test_is_usage_request_returns_false_for_other_commands() -> None:
    """Verifies is_usage_request returns False for non-usage commands."""
    adapter = TelegramAdapter(_make_config())
    event = NormalizedEvent(
        event_id="ev-new",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id="tg:123",
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id="trace-new",
        text="/new",
        metadata={"chat_id": 123},
    )
    assert adapter.is_usage_request(event) is False


class TestTelegramAdapterEgress:
    @pytest.mark.asyncio
    async def test_send_response_delegates_to_egress(self) -> None:
        adapter = TelegramAdapter(_make_config())
        with patch.object(adapter._egress, "send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            result = await adapter.send_response(_make_response(), chat_id=123456)
        assert result is True
        mock_send.assert_called_once()


class TestDelegationQuestionCallbacks:
    _CHAT_ID = 123456

    def _make_aq_event(self, callback_data: str) -> NormalizedEvent:
        return NormalizedEvent(
            event_id="ev-aq",
            event_type=EventType.USER_CALLBACK_QUERY,
            source=EventSource.TELEGRAM,
            session_id="tg:123456",
            user_id=str(self._CHAT_ID),
            created_at=datetime.now(UTC),
            trace_id="trace-aq",
            text=None,
            callback_query=CallbackQueryMeta(
                callback_id="cq-aq",
                callback_data=callback_data,
                origin_message_id=1,
                ui_version="1",
            ),
            metadata={"chat_id": self._CHAT_ID},
        )

    def _make_text_event(self) -> NormalizedEvent:
        return NormalizedEvent(
            event_id="ev-text",
            event_type=EventType.USER_TEXT_MESSAGE,
            source=EventSource.TELEGRAM,
            session_id="tg:123456",
            user_id=str(self._CHAT_ID),
            created_at=datetime.now(UTC),
            trace_id="trace-text",
            text="hello",
            metadata={"chat_id": self._CHAT_ID},
        )

    # --- build_delegation_question_response ---

    def test_build_with_options_returns_inline_keyboard(self) -> None:
        adapter = TelegramAdapter(_make_config())
        response = adapter.build_delegation_question_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456",
            trace_id="trace-1",
            question="Which option?",
            options=["Option A", "Option B"],
        )
        assert response.message_type == MessageType.INTERACTIVE
        assert response.ui_kind == "inline_keyboard"
        assert response.text == "Which option?"
        assert len(response.actions) == 2
        assert response.actions[0].label == "Option A"
        assert response.actions[1].label == "Option B"
        assert response.actions[0].callback_data.startswith("aq:")
        assert response.actions[1].callback_data.startswith("aq:")

    def test_build_without_options_returns_plain_text(self) -> None:
        adapter = TelegramAdapter(_make_config())
        response = adapter.build_delegation_question_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456",
            trace_id="trace-2",
            question="Please respond",
            options=[],
        )
        assert response.message_type == MessageType.TEXT
        assert response.ui_kind != "inline_keyboard"
        assert response.text == "Please respond"
        assert not response.actions

    def test_build_with_options_tokens_are_unique(self) -> None:
        adapter = TelegramAdapter(_make_config())
        response = adapter.build_delegation_question_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456",
            trace_id="trace-3",
            question="Pick one",
            options=["A", "B", "C"],
        )
        cb_data = [a.callback_data for a in response.actions]
        assert len(set(cb_data)) == 3

    # --- is_delegation_question_callback ---

    def test_is_delegation_question_callback_true_for_valid_aq(self) -> None:
        adapter = TelegramAdapter(_make_config())
        response = adapter.build_delegation_question_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456",
            trace_id="trace-4",
            question="Pick",
            options=["Yes", "No"],
        )
        event = self._make_aq_event(response.actions[0].callback_data)
        assert adapter.is_delegation_question_callback(event) is True

    def test_is_delegation_question_callback_false_for_mc_prefix(self) -> None:
        adapter = TelegramAdapter(_make_config())
        mc_response = adapter.build_memory_confirmation_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456",
            trace_id="trace-5",
            tool_call_id="tc-1",
            prompt_text="Confirm?",
        )
        event = self._make_aq_event(mc_response.actions[0].callback_data)
        assert adapter.is_delegation_question_callback(event) is False

    def test_is_delegation_question_callback_false_for_no_callback_query(self) -> None:
        adapter = TelegramAdapter(_make_config())
        assert adapter.is_delegation_question_callback(self._make_text_event()) is False

    def test_is_delegation_question_callback_false_for_other_prefix(self) -> None:
        adapter = TelegramAdapter(_make_config())
        event = self._make_aq_event("ms:some:callback:data:here")
        assert adapter.is_delegation_question_callback(event) is False

    # --- consume_delegation_question_callback ---

    def test_consume_returns_session_id_and_answer_for_valid_token(self) -> None:
        adapter = TelegramAdapter(_make_config())
        response = adapter.build_delegation_question_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456:abc",
            trace_id="trace-6",
            question="Which?",
            options=["Alpha"],
        )
        event = self._make_aq_event(response.actions[0].callback_data)
        result = adapter.consume_delegation_question_callback(event)
        assert result == ("tg:123456:abc", "Alpha")

    def test_consume_returns_none_for_tampered_token(self) -> None:
        adapter = TelegramAdapter(_make_config())
        response = adapter.build_delegation_question_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456",
            trace_id="trace-7",
            question="Choice?",
            options=["Beta"],
        )
        cb_data = response.actions[0].callback_data
        tampered = cb_data[:-1] + ("x" if cb_data[-1] != "x" else "y")
        event = self._make_aq_event(tampered)
        assert adapter.consume_delegation_question_callback(event) is None

    def test_consume_returns_none_for_expired_token(self) -> None:
        adapter = TelegramAdapter(_make_config())
        past_ts = int((datetime.now(UTC) - timedelta(hours=2)).timestamp())
        past_dt = datetime.fromtimestamp(past_ts, tz=UTC)
        with patch("assistant.channels.telegram.ask_question_callbacks.datetime") as mock_dt:
            mock_dt.now.return_value = past_dt
            response = adapter.build_delegation_question_response(
                chat_id=self._CHAT_ID,
                session_id="tg:123456",
                trace_id="trace-8",
                question="Old question?",
                options=["Gamma"],
            )
        event = self._make_aq_event(response.actions[0].callback_data)
        assert adapter.consume_delegation_question_callback(event) is None

    def test_consume_returns_none_on_second_call_replay_prevention(self) -> None:
        adapter = TelegramAdapter(_make_config())
        response = adapter.build_delegation_question_response(
            chat_id=self._CHAT_ID,
            session_id="tg:123456",
            trace_id="trace-9",
            question="Once only",
            options=["Delta"],
        )
        event = self._make_aq_event(response.actions[0].callback_data)
        first = adapter.consume_delegation_question_callback(event)
        assert first == ("tg:123456", "Delta")
        second = adapter.consume_delegation_question_callback(event)
        assert second is None

    def test_consume_returns_none_for_unknown_token(self) -> None:
        from assistant.channels.telegram.ask_question_callbacks import sign_ask_question_callback

        adapter = TelegramAdapter(_make_config())
        # Sign a valid callback without registering it in the adapter's token registry
        secret = b"12345:test-token"
        cb_data = sign_ask_question_callback(token="deadbeef01", chat_id=self._CHAT_ID, secret=secret)
        event = self._make_aq_event(cb_data)
        assert adapter.consume_delegation_question_callback(event) is None


class TestCapabilityOverrideSessionScoping:
    """Capability overrides must be session-scoped: cleared on every session transition."""

    _CHAT_ID = 42000
    _DEFAULT_CAPABILITIES = ["cap_a", "cap_b"]

    def _make_adapter(self) -> TelegramAdapter:
        return TelegramAdapter(
            _make_config(allowlist=[self._CHAT_ID]),
            default_capabilities=list(self._DEFAULT_CAPABILITIES),
        )

    def _make_text_event(self, text: str = "/new") -> NormalizedEvent:
        return NormalizedEvent(
            event_id="ev-1",
            event_type=EventType.USER_TEXT_MESSAGE,
            source=EventSource.TELEGRAM,
            session_id=f"tg:{self._CHAT_ID}:sess0",
            user_id=str(self._CHAT_ID),
            created_at=datetime.now(UTC),
            trace_id="trace-cap",
            text=text,
            metadata={"chat_id": self._CHAT_ID},
        )

    def _make_callback_event(self, callback_data: str) -> NormalizedEvent:
        return NormalizedEvent(
            event_id="ev-cb",
            event_type=EventType.USER_CALLBACK_QUERY,
            source=EventSource.TELEGRAM,
            session_id=f"tg:{self._CHAT_ID}:sess0",
            user_id=str(self._CHAT_ID),
            created_at=datetime.now(UTC),
            trace_id="trace-cap-cb",
            text=None,
            callback_query=CallbackQueryMeta(
                callback_id="cq-1",
                callback_data=callback_data,
                origin_message_id=1,
                ui_version="1",
            ),
            metadata={"chat_id": self._CHAT_ID},
        )

    def test_start_new_session_clears_capability_override(self) -> None:
        """start_new_session() must reset any dynamically-set capability override."""
        adapter = self._make_adapter()
        # Manually inject an override to simulate a prior dynamic activation
        context_id = f"telegram:{self._CHAT_ID}"
        adapter._capability_overrides[context_id] = ["cap_a", "cap_extra"]

        assert adapter.get_capabilities_override(self._CHAT_ID) == ["cap_a", "cap_extra"]

        adapter.start_new_session(self._make_text_event())

        assert adapter.get_capabilities_override(self._CHAT_ID) is None

    def test_handle_session_resume_callback_clears_capability_override(self) -> None:
        """handle_session_resume_callback() must reset any dynamically-set capability override."""
        from unittest.mock import MagicMock

        adapter = self._make_adapter()
        # Manually inject an override to simulate a prior dynamic activation
        context_id = f"telegram:{self._CHAT_ID}"
        adapter._capability_overrides[context_id] = ["cap_a", "cap_extra"]

        assert adapter.get_capabilities_override(self._CHAT_ID) is not None

        # Wire up a mock session_resume service that validates any callback
        resumed_session_id = f"tg:{self._CHAT_ID}:resumed"
        mock_session_resume = MagicMock()
        mock_session_resume.verify_callback.return_value = resumed_session_id
        adapter._session_resume = mock_session_resume

        # Stub out the active session context so set_active_session doesn't fail
        mock_active_ctx = MagicMock()
        adapter._active_session_context = mock_active_ctx

        event = self._make_callback_event("sr:fake-signed-payload")
        result = adapter.handle_session_resume_callback(event)

        assert result == resumed_session_id
        # Capability override must have been cleared
        assert adapter.get_capabilities_override(self._CHAT_ID) is None

    def test_capability_override_does_not_leak_to_new_session(self) -> None:
        """Full scenario: override set in session A must not be visible after /new."""
        adapter = self._make_adapter()
        context_id = f"telegram:{self._CHAT_ID}"

        # Simulate user toggling a capability in the current session
        adapter._capability_overrides[context_id] = ["cap_a", "cap_b", "cap_extra"]
        assert adapter.get_capabilities_override(self._CHAT_ID) == [
            "cap_a", "cap_b", "cap_extra"
        ]

        # User starts a new session
        adapter.start_new_session(self._make_text_event())

        # New session must have NO override (defaults from config apply)
        assert adapter.get_capabilities_override(self._CHAT_ID) is None

    def test_capability_override_does_not_leak_via_session_resume(self) -> None:
        """Full scenario: override set in session A must not be visible after resuming session B."""
        from unittest.mock import MagicMock

        adapter = self._make_adapter()
        context_id = f"telegram:{self._CHAT_ID}"

        # Simulate user toggling a capability in session A
        adapter._capability_overrides[context_id] = ["cap_a", "cap_extra"]
        assert adapter.get_capabilities_override(self._CHAT_ID) is not None

        # User resumes session B via /sessions
        mock_session_resume = MagicMock()
        mock_session_resume.verify_callback.return_value = f"tg:{self._CHAT_ID}:session_b"
        adapter._session_resume = mock_session_resume
        adapter._active_session_context = MagicMock()

        adapter.handle_session_resume_callback(self._make_callback_event("sr:payload"))

        # Resumed session B must have NO override carried over from session A
        assert adapter.get_capabilities_override(self._CHAT_ID) is None


class TestCapabilityOverrideResetScoping:
    """Capability overrides must be cleared when the active session is reset via /reset."""

    _CHAT_ID = 43000
    _DEFAULT_CAPABILITIES = ["cap_x", "cap_y"]

    def _make_adapter(self) -> TelegramAdapter:
        return TelegramAdapter(
            _make_config(allowlist=[self._CHAT_ID]),
            default_capabilities=list(self._DEFAULT_CAPABILITIES),
        )

    def _make_reset_event(self) -> NormalizedEvent:
        return NormalizedEvent(
            event_id="ev-reset-1",
            event_type=EventType.USER_TEXT_MESSAGE,
            source=EventSource.TELEGRAM,
            session_id=f"tg:{self._CHAT_ID}:sess0",
            user_id=str(self._CHAT_ID),
            created_at=datetime.now(UTC),
            trace_id="trace-reset",
            text="/reset",
            metadata={"chat_id": self._CHAT_ID},
        )

    @pytest.mark.asyncio
    async def test_reset_session_context_clears_capability_override(self) -> None:
        """reset_session_context() must clear any dynamically-set capability override."""
        from unittest.mock import AsyncMock

        adapter = self._make_adapter()
        # Manually inject an override to simulate a prior dynamic capability activation
        context_id = f"telegram:{self._CHAT_ID}"
        adapter._capability_overrides[context_id] = ["cap_x", "cap_extra"]

        assert adapter.get_capabilities_override(self._CHAT_ID) == ["cap_x", "cap_extra"]

        # Wire up a mock session store so reset_session_context proceeds
        mock_store = AsyncMock()
        mock_store.clear_session.return_value = True
        adapter._session_store = mock_store

        await adapter.reset_session_context(self._make_reset_event())

        # Capability override must have been cleared after /reset
        assert adapter.get_capabilities_override(self._CHAT_ID) is None

    @pytest.mark.asyncio
    async def test_capability_override_does_not_persist_after_reset(self) -> None:
        """Full scenario: override set before /reset must not be visible after session reset."""
        from unittest.mock import AsyncMock

        adapter = self._make_adapter()
        context_id = f"telegram:{self._CHAT_ID}"

        # Simulate user toggling a capability in the current session
        adapter._capability_overrides[context_id] = ["cap_x", "cap_y", "cap_extra"]
        assert adapter.get_capabilities_override(self._CHAT_ID) == [
            "cap_x", "cap_y", "cap_extra"
        ]

        # User runs /reset to clear the session context
        mock_store = AsyncMock()
        mock_store.clear_session.return_value = True
        adapter._session_store = mock_store

        cleared = await adapter.reset_session_context(self._make_reset_event())

        assert cleared is True
        # After reset, capabilities must fall back to config defaults (no override)
        assert adapter.get_capabilities_override(self._CHAT_ID) is None

    @pytest.mark.asyncio
    async def test_reset_without_session_store_does_not_clear_override(self) -> None:
        """When session reset is unavailable, capability override state is unchanged."""
        adapter = self._make_adapter()
        context_id = f"telegram:{self._CHAT_ID}"

        adapter._capability_overrides[context_id] = ["cap_x", "cap_extra"]
        assert adapter.get_capabilities_override(self._CHAT_ID) is not None

        # No session store configured — reset is unavailable
        adapter._session_store = None

        result = await adapter.reset_session_context(self._make_reset_event())

        # reset_session_context returns False and must not touch overrides
        assert result is False
        # Override is still present since reset was a no-op
        assert adapter.get_capabilities_override(self._CHAT_ID) == ["cap_x", "cap_extra"]
