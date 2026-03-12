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
    _extract_raw_text_for_multimodal,
    _extract_user_text,
    _format_attachment_context,
    _gather_attachments,
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


class TestExtractRawTextForMultimodal:
    def test_returns_none_for_attachment_only(self) -> None:
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={
                "attachment": AttachmentMeta(
                    file_id="f1",
                    mime_type="image/jpeg",
                    file_size_bytes=100,
                    caption=None,
                )
            }
        )
        assert _extract_raw_text_for_multimodal(event) is None

    def test_returns_caption_when_present(self) -> None:
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={
                "attachment": AttachmentMeta(
                    file_id="f1",
                    mime_type="image/jpeg",
                    file_size_bytes=100,
                    caption="check this!",
                )
            }
        )
        assert _extract_raw_text_for_multimodal(event) == "check this!"


class TestGatherAttachments:
    def test_single_attachment(self) -> None:
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={
                "attachment": AttachmentMeta(
                    file_id="f1",
                    mime_type="image/jpeg",
                    file_size_bytes=1024,
                    caption=None,
                )
            }
        )
        assert _gather_attachments(event) == [event.attachment]

    def test_skips_empty_file_id(self) -> None:
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={"attachment": AttachmentMeta(file_id="", mime_type="x", file_size_bytes=0)}
        )
        assert _gather_attachments(event) == []


class TestFormatAttachmentContext:
    def test_empty(self) -> None:
        assert _format_attachment_context([]) == ""

    def test_single_image(self) -> None:
        att = AttachmentMeta(
            file_id="f1",
            mime_type="image/jpeg",
            file_size_bytes=1024,
            caption=None,
        )
        assert "[User attached:" in _format_attachment_context([att])
        assert "image" in _format_attachment_context([att])
        assert "image/jpeg" in _format_attachment_context([att])


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
    async def test_new_session_calls_provider_with_first_user_message(
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
        assert result == "Model response"
        mock_provider.complete.assert_called_once()
        call_args = mock_provider.complete.call_args[0][0]
        assert len(call_args.messages) == 1
        assert call_args.messages[0].content == "Hello"
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
        assert append_call[0].payload["attachments"] == []
        assert append_call[1].record_type == SessionRecordType.ASSISTANT_MESSAGE
        assert append_call[1].payload["content"] == "Model response"
        assert append_call[2].record_type == SessionRecordType.TURN_TERMINAL

    @pytest.mark.asyncio
    async def test_attachment_reaches_llm(
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
        event = _minimal_event(text="check this!")
        event = event.model_copy(
            update={
                "event_type": EventType.USER_ATTACHMENT_MESSAGE,
                "attachment": AttachmentMeta(
                    file_id="photo-123",
                    mime_type="image/jpeg",
                    file_size_bytes=45000,
                    caption=None,
                ),
            }
        )
        await orch.execute_turn(event)
        call_args = mock_provider.complete.call_args[0][0]
        assert len(call_args.messages) == 1
        content = call_args.messages[0].content
        assert "check this!" in content
        assert "[User attached:" in content
        assert "image" in content
        assert "image/jpeg" in content
        append_call = mock_store.sessions.append.call_args[0][0]
        assert append_call[0].payload["attachments"] == [
            {
                "file_id": "photo-123",
                "mime_type": "image/jpeg",
                "file_size_bytes": 45000,
                "file_name": None,
                "caption": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_attachment_with_downloader_sends_content_blocks(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])

        async def mock_download(
            file_id: str, mime_type: str, file_size_bytes: int, trace_id: str
        ) -> bytes | None:
            return b"\xff\xd8\xff"  # minimal JPEG header

        mock_downloader = MagicMock()
        mock_downloader.download = AsyncMock(side_effect=mock_download)

        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
        )
        event = _minimal_event(text="check this!")
        event = event.model_copy(
            update={
                "event_type": EventType.USER_ATTACHMENT_MESSAGE,
                "attachment": AttachmentMeta(
                    file_id="photo-123",
                    mime_type="image/jpeg",
                    file_size_bytes=100,
                    caption=None,
                ),
            }
        )
        await orch.execute_turn(event)
        call_args = mock_provider.complete.call_args[0][0]
        assert len(call_args.messages) == 1
        msg = call_args.messages[0]
        assert msg.content_blocks is not None
        assert len(msg.content_blocks) == 2
        assert msg.content_blocks[0] == {"type": "text", "text": "check this!"}
        assert msg.content_blocks[1]["type"] == "image"
        assert msg.content_blocks[1]["source"]["type"] == "base64"
        assert msg.content_blocks[1]["source"]["media_type"] == "image/jpeg"

    @pytest.mark.asyncio
    async def test_attachment_only_no_placeholder_in_content_blocks(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])

        async def mock_download(
            file_id: str, mime_type: str, file_size_bytes: int, trace_id: str
        ) -> bytes | None:
            return b"\xff\xd8\xff"

        mock_downloader = MagicMock()
        mock_downloader.download = AsyncMock(side_effect=mock_download)

        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
        )
        event = _minimal_event(text=None)
        event = event.model_copy(
            update={
                "event_type": EventType.USER_ATTACHMENT_MESSAGE,
                "attachment": AttachmentMeta(
                    file_id="photo-123",
                    mime_type="image/jpeg",
                    file_size_bytes=100,
                    caption=None,
                ),
            }
        )
        await orch.execute_turn(event)
        call_args = mock_provider.complete.call_args[0][0]
        msg = call_args.messages[0]
        assert msg.content_blocks is not None
        assert len(msg.content_blocks) == 1
        assert msg.content_blocks[0]["type"] == "image"
        assert "[Empty or unsupported input]" not in str(msg.content_blocks)

    @pytest.mark.asyncio
    async def test_text_attachment_with_downloader_adds_text_block(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])

        async def mock_download(
            file_id: str, mime_type: str, file_size_bytes: int, trace_id: str
        ) -> bytes | None:
            return b"# Architecture improvements\n\n- Add retries\n- Add metrics\n"

        mock_downloader = MagicMock()
        mock_downloader.download = AsyncMock(side_effect=mock_download)

        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
        )
        event = _minimal_event(text="Check this out!")
        event = event.model_copy(
            update={
                "event_type": EventType.USER_ATTACHMENT_MESSAGE,
                "attachment": AttachmentMeta(
                    file_id="doc-321",
                    mime_type="text/markdown",
                    file_size_bytes=7_600,
                    caption=None,
                ),
            }
        )
        await orch.execute_turn(event)

        call_args = mock_provider.complete.call_args[0][0]
        msg = call_args.messages[0]
        assert msg.content_blocks is not None
        assert len(msg.content_blocks) == 2
        assert msg.content_blocks[0] == {"type": "text", "text": "Check this out!"}
        assert msg.content_blocks[1]["type"] == "text"
        assert "Attachment content: text/markdown" in msg.content_blocks[1]["text"]
        assert "Architecture improvements" in msg.content_blocks[1]["text"]
        assert "[Empty or unsupported input]" not in str(msg.content_blocks)

    @pytest.mark.asyncio
    async def test_octet_stream_markdown_filename_is_treated_as_text_attachment(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_provider: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])

        async def mock_download(
            file_id: str, mime_type: str, file_size_bytes: int, trace_id: str
        ) -> bytes | None:
            return b"# Design notes\n\nThe attachment was decoded."

        mock_downloader = MagicMock()
        mock_downloader.download = AsyncMock(side_effect=mock_download)

        orch = Orchestrator(
            store=mock_store,
            provider=mock_provider,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
        )
        event = _minimal_event(text="Please review this")
        event = event.model_copy(
            update={
                "event_type": EventType.USER_ATTACHMENT_MESSAGE,
                "attachment": AttachmentMeta(
                    file_id="doc-999",
                    mime_type="application/octet-stream",
                    file_size_bytes=7_600,
                    file_name="architecture_improvements_exercise.md",
                    caption=None,
                ),
            }
        )
        await orch.execute_turn(event)

        call_args = mock_provider.complete.call_args[0][0]
        msg = call_args.messages[0]
        assert msg.content_blocks is not None
        assert len(msg.content_blocks) == 2
        assert msg.content_blocks[1]["type"] == "text"
        assert (
            "application/octet-stream (architecture_improvements_exercise.md)"
            in (msg.content_blocks[1]["text"])
        )
        assert "Design notes" in msg.content_blocks[1]["text"]
