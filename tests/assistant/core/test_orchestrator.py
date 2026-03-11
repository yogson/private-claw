"""
Tests for CMP_CORE_AGENT_ORCHESTRATOR orchestrator module.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesConfig,
    McpServersConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
)
from assistant.core.events.models import (
    AttachmentMeta,
    CallbackQueryMeta,
    EventSource,
    EventType,
    OrchestratorEvent,
    VoiceMeta,
)
from assistant.core.orchestrator import (
    Orchestrator,
    _extract_user_text,
    _records_to_messages,
)
from assistant.providers.interfaces import LLMResponse, MessageRole
from assistant.store.models import SessionRecord, SessionRecordType


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id="claude-3-5-sonnet-20241022",
            model_allowlist=["claude-3-5-sonnet-20241022"],
            max_tokens_default=1024,
        ),
        capabilities=CapabilitiesConfig(allowed_capabilities=[]),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
    )


def _minimal_event(
    event_id: str = "ev-1",
    text: str | None = "Hello",
    session_id: str = "tg:123",
    trace_id: str = "trace-1",
) -> OrchestratorEvent:
    return OrchestratorEvent(
        event_id=event_id,
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id="123",
        created_at=datetime.now(UTC),
        trace_id=trace_id,
        text=text,
    )


class TestExtractUserText:
    def test_text_message(self) -> None:
        event = _minimal_event(text="  Hello world  ")
        assert _extract_user_text(event) == "Hello world"

    def test_voice_with_transcript(self) -> None:
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={
                "voice": VoiceMeta(
                    file_id="f1",
                    duration_seconds=5,
                    transcript_text=" Voice text ",
                )
            }
        )
        assert _extract_user_text(event) == "Voice text"

    def test_attachment_with_caption(self) -> None:
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={
                "attachment": AttachmentMeta(
                    file_id="f1",
                    mime_type="image/png",
                    file_size_bytes=100,
                    caption=" Caption here ",
                )
            }
        )
        assert _extract_user_text(event) == "Caption here"

    def test_callback_query(self) -> None:
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={
                "callback_query": CallbackQueryMeta(
                    callback_id="cb1", callback_data="resume_session:123:tg:123:0:abc"
                )
            }
        )
        assert "[Callback:" in _extract_user_text(event)

    def test_empty_fallback(self) -> None:
        event = _minimal_event(text=None)
        assert _extract_user_text(event) == "[Empty or unsupported input]"


class TestRecordsToMessages:
    def test_empty_records(self) -> None:
        assert _records_to_messages([]) == []

    def test_user_and_assistant(self) -> None:
        records = [
            SessionRecord(
                session_id="s1",
                sequence=0,
                event_id="e1",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": "m1", "content": "Hi"},
            ),
            SessionRecord(
                session_id="s1",
                sequence=1,
                event_id="e2",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_MESSAGE,
                payload={"message_id": "m2", "content": "Hello!"},
            ),
        ]
        msgs = _records_to_messages(records)
        assert len(msgs) == 2
        assert msgs[0].role == MessageRole.USER and msgs[0].content == "Hi"
        assert msgs[1].role == MessageRole.ASSISTANT and msgs[1].content == "Hello!"

    def test_skips_empty_content(self) -> None:
        records = [
            SessionRecord(
                session_id="s1",
                sequence=0,
                event_id="e1",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": "m1", "content": ""},
            ),
        ]
        assert _records_to_messages(records) == []


@pytest.fixture
def mock_store() -> MagicMock:
    store = MagicMock()
    store.sessions.replay_for_turn = AsyncMock(return_value=[])
    store.sessions.session_exists = AsyncMock(return_value=False)
    store.sessions.get_next_sequence = AsyncMock(return_value=0)
    store.sessions.append = AsyncMock()
    store.locks.lock = MagicMock()
    store.locks.lock.return_value.__aenter__ = AsyncMock(return_value=None)
    store.locks.lock.return_value.__aexit__ = AsyncMock(return_value=None)
    return store


@pytest.fixture
def mock_idempotency() -> MagicMock:
    svc = MagicMock()
    svc.build_key = lambda s, e: f"{s}:{e}"
    svc.check_and_register = AsyncMock(return_value=(False, None))
    return svc


@pytest.fixture
def mock_provider() -> MagicMock:
    prov = MagicMock()
    prov.complete = AsyncMock(
        return_value=LLMResponse(
            text="Model response",
            model_id="claude-3-5-sonnet-20241022",
            trace_id="trace-1",
        )
    )
    return prov


class TestOrchestratorExecuteTurn:
    @pytest.mark.asyncio
    async def test_duplicate_returns_none(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        mock_idempotency.check_and_register = AsyncMock(return_value=(True, MagicMock()))
        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
        )
        event = _minimal_event()
        result = await orch.execute_turn(event)
        assert result is None
        mock_provider.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_session_returns_greeting(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
        )
        event = _minimal_event()
        result = await orch.execute_turn(event)
        assert result == "Hello! How can I help you today?"
        mock_provider.complete.assert_not_called()
        mock_store.sessions.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_session_calls_provider(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])
        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
        )
        event = _minimal_event(text="What is 2+2?")
        result = await orch.execute_turn(event)
        assert result == "Model response"
        mock_provider.complete.assert_called_once()
        call_args = mock_provider.complete.call_args[0][0]
        assert len(call_args.messages) == 1
        assert call_args.messages[0].content == "What is 2+2?"
        mock_store.sessions.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_persists_turn_artifacts(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
        )
        event = _minimal_event(event_id="ev-99", text="Hi")
        await orch.execute_turn(event)
        append_call = mock_store.sessions.append.call_args[0][0]
        assert len(append_call) == 3
        assert append_call[0].record_type == SessionRecordType.USER_MESSAGE
        assert append_call[0].payload["content"] == "Hi"
        assert append_call[1].record_type == SessionRecordType.ASSISTANT_MESSAGE
        assert append_call[1].payload["content"] == "Model response"
        assert append_call[2].record_type == SessionRecordType.TURN_TERMINAL
