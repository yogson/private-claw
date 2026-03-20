"""
Tests for CMP_CORE_AGENT_ORCHESTRATOR orchestrator module.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from assistant.agent.interfaces import MessageRole
from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolsConfig,
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
from assistant.core.orchestrator.service import MEMORY_SEARCH_MATCH_BODY_MAX_CHARS
from assistant.memory.retrieval.models import RetrievalAudit, RetrievalResult, ScoredArtifact
from assistant.memory.store.models import MemoryArtifact, MemoryFrontmatter, MemoryType
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
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant"],
            denied_capabilities=[],
        ),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(lock_ttl_seconds=30, idempotency_retention_seconds=86400),
        memory=MemoryConfig(api_key="test"),
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

    def test_tool_call_and_result_included(self) -> None:
        """Replay mapping includes assistant tool-use and user tool-result blocks."""
        records = [
            SessionRecord(
                session_id="s1",
                sequence=0,
                event_id="e1",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": "m1", "content": "Remember X"},
            ),
            SessionRecord(
                session_id="s1",
                sequence=1,
                event_id="e2",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_MESSAGE,
                payload={"message_id": "m2", "content": "I'll remember that."},
            ),
            SessionRecord(
                session_id="s1",
                sequence=2,
                event_id="e3",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
                payload={
                    "message_id": "m3",
                    "tool_call_id": "call-1",
                    "tool_name": "memory_search",
                    "arguments_json": '{"query":"deadlift notes","limit":3}',
                },
            ),
            SessionRecord(
                session_id="s1",
                sequence=3,
                event_id="e4",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.TOOL_RESULT,
                payload={
                    "message_id": "m4",
                    "tool_call_id": "call-1",
                    "tool_name": "memory_search",
                    "result": {"status": "ok", "matches": [{"body": "deadlift"}]},
                    "error": None,
                },
            ),
        ]
        msgs = _records_to_messages(records)
        assert len(msgs) == 3
        assert msgs[0].role == MessageRole.USER and msgs[0].content == "Remember X"
        assert msgs[1].role == MessageRole.ASSISTANT
        assert msgs[1].content_blocks is not None
        assert any(b.get("type") == "tool_use" for b in msgs[1].content_blocks)
        assert msgs[2].role == MessageRole.USER
        assert msgs[2].content_blocks is not None
        assert any(b.get("type") == "tool_result" for b in msgs[2].content_blocks)

    def test_deduplicates_tool_results_for_same_tool_call_id(self) -> None:
        """Emit only the last TOOL_RESULT for repeated tool_call_id."""
        records = [
            SessionRecord(
                session_id="s1",
                sequence=0,
                event_id="e1",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": "m1", "content": "Remember my name"},
            ),
            SessionRecord(
                session_id="s1",
                sequence=1,
                event_id="e2",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
                payload={
                    "message_id": "m2",
                    "tool_call_id": "call-1",
                    "tool_name": "memory_search",
                    "arguments_json": '{"query":"name","limit":1}',
                },
            ),
            SessionRecord(
                session_id="s1",
                sequence=2,
                event_id="e3",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.TOOL_RESULT,
                payload={
                    "message_id": "m3",
                    "tool_call_id": "call-1",
                    "tool_name": "memory_search",
                    "result": {
                        "status": "ok",
                        "matches": [{"body": "Egor"}],
                    },
                    "error": None,
                },
            ),
            SessionRecord(
                session_id="s1",
                sequence=3,
                event_id="e4",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.TOOL_RESULT,
                payload={
                    "message_id": "m4",
                    "tool_call_id": "call-1",
                    "tool_name": "memory_search",
                    "result": {"status": "ok", "matches": [{"body": "Egor M."}]},
                    "error": None,
                },
            ),
        ]
        msgs = _records_to_messages(records)
        assert len(msgs) == 3  # user, assistant (tool_use), user (tool_result)
        tool_result_blocks = [
            b for b in (msgs[2].content_blocks or []) if b.get("type") == "tool_result"
        ]
        assert len(tool_result_blocks) == 1
        content = json.loads(tool_result_blocks[0]["content"])
        assert content["status"] == "ok"
        assert content.get("matches") == [{"body": "Egor M."}]

    def test_skips_memory_propose_update_blocks_from_replay(self) -> None:
        records = [
            SessionRecord(
                session_id="s1",
                sequence=0,
                event_id="e1",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.USER_MESSAGE,
                payload={"message_id": "m1", "content": "Remember this"},
            ),
            SessionRecord(
                session_id="s1",
                sequence=1,
                event_id="e2",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_MESSAGE,
                payload={"message_id": "m2", "content": "Saved."},
            ),
            SessionRecord(
                session_id="s1",
                sequence=2,
                event_id="e3",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
                payload={
                    "message_id": "m3",
                    "tool_call_id": "call-mem-1",
                    "tool_name": "memory_propose_update",
                    "arguments_json": '{"intent_id":"x","action":"create"}',
                },
            ),
            SessionRecord(
                session_id="s1",
                sequence=3,
                event_id="e4",
                turn_id="t1",
                timestamp=datetime.now(UTC),
                record_type=SessionRecordType.TOOL_RESULT,
                payload={
                    "message_id": "m4",
                    "tool_call_id": "call-mem-1",
                    "tool_name": "memory_propose_update",
                    "result": {"status": "written", "memory_id": "mem-1"},
                    "error": None,
                },
            ),
        ]
        msgs = _records_to_messages(records)
        assert len(msgs) == 2
        assert msgs[0].role == MessageRole.USER
        assert msgs[1].role == MessageRole.ASSISTANT
        assert msgs[1].content == "Saved."


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
def mock_pydantic_adapter() -> MagicMock:
    adapter = MagicMock(spec=PydanticAITurnAdapter)
    adapter.run_turn = AsyncMock(return_value=("Model response", [], None))
    return adapter


class TestOrchestratorExecuteTurn:
    @pytest.mark.asyncio
    async def test_duplicate_returns_none(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_idempotency.check_and_register = AsyncMock(return_value=(True, MagicMock()))
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        event = _minimal_event()
        result = await orch.execute_turn(event)
        assert result is None
        mock_pydantic_adapter.run_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_session_calls_provider_with_first_user_message(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        event = _minimal_event()
        result = await orch.execute_turn(event)
        assert result is not None and result.text == "Model response"
        mock_pydantic_adapter.run_turn.assert_called_once()
        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        assert len(call_kwargs["messages"]) == 1
        assert call_kwargs["messages"][0]["content"] == "Hello"
        assert mock_store.sessions.append.call_count == 2

    @pytest.mark.asyncio
    async def test_existing_session_calls_provider(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        event = _minimal_event(text="What is 2+2?")
        result = await orch.execute_turn(event)
        assert result is not None and result.text == "Model response"
        mock_pydantic_adapter.run_turn.assert_called_once()
        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        assert len(call_kwargs["messages"]) == 1
        assert call_kwargs["messages"][0]["content"] == "What is 2+2?"
        assert mock_store.sessions.append.call_count == 2

    @pytest.mark.asyncio
    async def test_memory_search_handler_is_exposed_to_agent_deps(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])
        retrieval_service = MagicMock()
        retrieval_service.retrieve.return_value = RetrievalResult(
            scored_artifacts=[
                ScoredArtifact(
                    artifact=MemoryArtifact(
                        frontmatter=MemoryFrontmatter(
                            memory_id="profile-1",
                            type=MemoryType.PROFILE,
                            tags=["user_profile"],
                            entities=["Egor"],
                            priority=5,
                            confidence=1.0,
                            updated_at=datetime.now(UTC),
                            last_used_at=None,
                            created_at=datetime.now(UTC),
                        ),
                        body="- name: Egor",
                    ),
                    score=0.95,
                )
            ],
            audit=RetrievalAudit(),
        )
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            memory_retrieval=retrieval_service,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        event = _minimal_event(text="Do you know my name?")
        await orch.execute_turn(event)

        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        assert len(call_kwargs["messages"]) == 1
        assert call_kwargs["messages"][0]["content"] == "Do you know my name?"
        deps = call_kwargs["deps"]
        assert deps.memory_search_handler is not None
        tool_result = deps.memory_search_handler("my name", 3, ["profile"])
        assert tool_result["status"] == "ok"
        assert tool_result["matches"]
        assert tool_result["matches"][0]["body"] == "- name: Egor"
        retrieval_service.retrieve.assert_called_once()

    @pytest.mark.asyncio
    async def test_memory_search_truncates_very_long_match_bodies(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        long_body = "x" * (MEMORY_SEARCH_MATCH_BODY_MAX_CHARS + 50)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])
        retrieval_service = MagicMock()
        retrieval_service.retrieve.return_value = RetrievalResult(
            scored_artifacts=[
                ScoredArtifact(
                    artifact=MemoryArtifact(
                        frontmatter=MemoryFrontmatter(
                            memory_id="m-1",
                            type=MemoryType.PROJECTS,
                            tags=[],
                            entities=[],
                            priority=1,
                            confidence=1.0,
                            updated_at=datetime.now(UTC),
                            last_used_at=None,
                            created_at=datetime.now(UTC),
                        ),
                        body=long_body,
                    ),
                    score=0.9,
                )
            ],
            audit=RetrievalAudit(),
        )
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            memory_retrieval=retrieval_service,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        await orch.execute_turn(_minimal_event(text="q"))
        deps = mock_pydantic_adapter.run_turn.call_args.kwargs["deps"]
        tool_result = deps.memory_search_handler("q", 3, None)
        got = tool_result["matches"][0]["body"]
        assert got.endswith("... [truncated]")
        assert len(got) == MEMORY_SEARCH_MATCH_BODY_MAX_CHARS + len("... [truncated]")

    @pytest.mark.asyncio
    async def test_prompt_trace_is_persisted_when_enabled(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        cfg = _runtime_config()
        cfg.model.prompt_trace_enabled = True
        mock_pydantic_adapter.system_prompt = "System trace prompt"
        orch = Orchestrator(
            store=mock_store,
            config=cfg,
            idempotency=mock_idempotency,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        await orch.execute_turn(_minimal_event(text="trace me"))
        initial_records = mock_store.sessions.append.call_args_list[0][0][0]
        summaries = [r for r in initial_records if r.record_type == SessionRecordType.TURN_SUMMARY]
        assert len(summaries) == 1
        prompt_trace = summaries[0].payload["capability_audit"]["prompt_trace"]
        assert prompt_trace["system_prompt"] == "System trace prompt"
        assert prompt_trace["user_prompt"]["content"] == "trace me"

    @pytest.mark.asyncio
    async def test_persists_turn_artifacts(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        event = _minimal_event(event_id="ev-99", text="Hi")
        await orch.execute_turn(event)
        initial_records = mock_store.sessions.append.call_args_list[0][0][0]
        final_records = mock_store.sessions.append.call_args_list[1][0][0]
        assert len(initial_records) == 2
        assert initial_records[0].record_type == SessionRecordType.USER_MESSAGE
        assert initial_records[0].payload["content"] == "Hi"
        assert initial_records[0].payload["attachments"] == []
        assert initial_records[1].record_type == SessionRecordType.ASSISTANT_MESSAGE
        assert initial_records[1].payload["content"] == "Model response"
        assert len(final_records) == 1
        assert final_records[0].record_type == SessionRecordType.TURN_TERMINAL

    @pytest.mark.asyncio
    async def test_attachment_reaches_llm(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_store.sessions.session_exists = AsyncMock(return_value=True)
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            pydantic_ai_adapter=mock_pydantic_adapter,
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
        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        assert len(call_kwargs["messages"]) == 1
        content = call_kwargs["messages"][0]["content"]
        assert "check this!" in content
        assert "[User attached:" in content
        assert "image" in content
        assert "image/jpeg" in content
        initial_records = mock_store.sessions.append.call_args_list[0][0][0]
        assert initial_records[0].payload["attachments"] == [
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
        mock_pydantic_adapter: MagicMock,
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
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
            pydantic_ai_adapter=mock_pydantic_adapter,
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
        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        assert len(call_kwargs["messages"]) == 1
        msg = call_kwargs["messages"][0]
        assert msg["content_blocks"] is not None
        assert len(msg["content_blocks"]) == 2
        assert msg["content_blocks"][0] == {"type": "text", "text": "check this!"}
        assert msg["content_blocks"][1]["type"] == "image"
        assert msg["content_blocks"][1]["source"]["type"] == "base64"
        assert msg["content_blocks"][1]["source"]["media_type"] == "image/jpeg"

    @pytest.mark.asyncio
    async def test_attachment_only_no_placeholder_in_content_blocks(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
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
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
            pydantic_ai_adapter=mock_pydantic_adapter,
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
        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        msg = call_kwargs["messages"][0]
        assert msg["content_blocks"] is not None
        assert len(msg["content_blocks"]) == 1
        assert msg["content_blocks"][0]["type"] == "image"
        assert "[Empty or unsupported input]" not in str(msg["content_blocks"])

    @pytest.mark.asyncio
    async def test_text_attachment_with_downloader_adds_text_block(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
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
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
            pydantic_ai_adapter=mock_pydantic_adapter,
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

        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        msg = call_kwargs["messages"][0]
        assert msg["content_blocks"] is not None
        assert len(msg["content_blocks"]) == 2
        assert msg["content_blocks"][0] == {"type": "text", "text": "Check this out!"}
        assert msg["content_blocks"][1]["type"] == "text"
        assert "Attachment content: text/markdown" in msg["content_blocks"][1]["text"]
        assert "Architecture improvements" in msg["content_blocks"][1]["text"]
        assert "[Empty or unsupported input]" not in str(msg["content_blocks"])

    @pytest.mark.asyncio
    async def test_octet_stream_markdown_filename_is_treated_as_text_attachment(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
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
            config=_runtime_config(),
            idempotency=mock_idempotency,
            attachment_downloader=mock_downloader,
            pydantic_ai_adapter=mock_pydantic_adapter,
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

        call_kwargs = mock_pydantic_adapter.run_turn.call_args.kwargs
        msg = call_kwargs["messages"][0]
        assert msg["content_blocks"] is not None
        assert len(msg["content_blocks"]) == 2
        assert msg["content_blocks"][1]["type"] == "text"
        assert (
            "application/octet-stream (architecture_improvements_exercise.md)"
            in (msg["content_blocks"][1]["text"])
        )
        assert "Design notes" in msg["content_blocks"][1]["text"]

    @pytest.mark.asyncio
    async def test_structured_memory_intents_are_applied_and_persisted(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])
        mock_pydantic_adapter.run_turn = AsyncMock(
            return_value=(
                "Noted your preference.",
                [
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="memory_propose_update",
                                args={
                                    "intent_id": "intent-pref-1",
                                    "action": "upsert",
                                    "memory_type": "preferences",
                                    "candidate": {
                                        "tags": ["style"],
                                        "entities": [],
                                        "priority": 6,
                                        "confidence": 0.9,
                                        "body_markdown": "User prefers concise responses.",
                                    },
                                    "reason": "explicit ask",
                                    "source": "explicit_user_request",
                                    "requires_user_confirmation": False,
                                },
                                tool_call_id="tool-1",
                            )
                        ]
                    ),
                    ModelRequest(
                        parts=[
                            ToolReturnPart(
                                tool_name="memory_propose_update",
                                tool_call_id="tool-1",
                                content='{"status":"approved_pending_apply","reason":"","requires_user_confirmation":false}',
                            )
                        ]
                    ),
                ],
                None,
            )
        )
        memory_writer = MagicMock()
        memory_writer.apply_intent.return_value.model_dump_json.return_value = json.dumps(
            {
                "intent_id": "intent-pref-1",
                "status": "written",
                "memory_id": "preferences-abc",
                "reason": "",
            }
        )

        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            memory_writer=memory_writer,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )

        event = _minimal_event(text="Please remember I prefer concise replies.")
        result = await orch.execute_turn(event)

        assert result is not None and result.text == "Noted your preference."
        mock_pydantic_adapter.run_turn.assert_called_once()
        memory_writer.apply_intent.assert_called_once()
        appended_records = [
            rec for call in mock_store.sessions.append.call_args_list for rec in call[0][0]
        ]
        record_types = [r.record_type for r in appended_records]
        assert SessionRecordType.ASSISTANT_TOOL_CALL in record_types
        assert SessionRecordType.TOOL_RESULT in record_types
        written = [
            rec
            for rec in appended_records
            if rec.record_type == SessionRecordType.TOOL_RESULT
            and rec.payload.get("result", {}).get("status") == "written"
        ]
        assert written

    @pytest.mark.asyncio
    async def test_confirmation_required_intent_is_not_applied(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_pydantic_adapter.run_turn = AsyncMock(
            return_value=(
                "Please confirm this memory update.",
                [
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="memory_propose_update",
                                args={
                                    "intent_id": "intent-pref-2",
                                    "action": "upsert",
                                    "memory_type": "preferences",
                                    "candidate": {
                                        "tags": ["style"],
                                        "entities": [],
                                        "priority": 6,
                                        "confidence": 0.9,
                                        "body_markdown": "Keep responses concise.",
                                    },
                                    "reason": "explicit ask",
                                    "source": "explicit_user_request",
                                    "requires_user_confirmation": True,
                                },
                                tool_call_id="tool-2",
                            )
                        ]
                    ),
                    ModelRequest(
                        parts=[
                            ToolReturnPart(
                                tool_name="memory_propose_update",
                                tool_call_id="tool-2",
                                content='{"status":"pending_confirmation","reason":"requires_user_confirmation=true","requires_user_confirmation":true}',
                            )
                        ]
                    ),
                ],
                None,
            )
        )
        memory_writer = MagicMock()
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            memory_writer=memory_writer,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )

        result = await orch.execute_turn(_minimal_event(text="remember this"))
        assert result is not None and result.text == "Please confirm this memory update."
        memory_writer.apply_intent.assert_not_called()
        all_records = [
            rec for call in mock_store.sessions.append.call_args_list for rec in call[0][0]
        ]
        pending = [
            rec
            for rec in all_records
            if rec.record_type == SessionRecordType.TOOL_RESULT
            and rec.payload.get("result", {}).get("status") == "pending_confirmation"
        ]
        assert pending

    @pytest.mark.asyncio
    async def test_plain_json_text_does_not_trigger_memory_write(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_pydantic_adapter.run_turn = AsyncMock(
            return_value=('{"memory_update_intents":[{"intent_id":"x"}]}', [], None)
        )
        memory_writer = MagicMock()
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            memory_writer=memory_writer,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )
        result = await orch.execute_turn(_minimal_event(text="remember this"))
        assert result is not None and result.text == '{"memory_update_intents":[{"intent_id":"x"}]}'
        memory_writer.apply_intent.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_memory_write_when_initial_persist_fails(
        self,
        mock_store: MagicMock,
        mock_idempotency: MagicMock,
        mock_pydantic_adapter: MagicMock,
    ) -> None:
        mock_store.sessions.replay_for_turn = AsyncMock(return_value=[])
        mock_store.sessions.append = AsyncMock(side_effect=RuntimeError("persist failed"))
        mock_pydantic_adapter.run_turn = AsyncMock(
            return_value=(
                "Will remember this.",
                [
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="memory_propose_update",
                                args={
                                    "intent_id": "intent-pref-3",
                                    "action": "upsert",
                                    "memory_type": "preferences",
                                    "candidate": {
                                        "tags": ["style"],
                                        "entities": [],
                                        "priority": 5,
                                        "confidence": 0.9,
                                        "body_markdown": "Use concise answers.",
                                    },
                                    "reason": "explicit ask",
                                    "source": "explicit_user_request",
                                    "requires_user_confirmation": False,
                                },
                                tool_call_id="tool-3",
                            )
                        ]
                    ),
                    ModelRequest(
                        parts=[
                            ToolReturnPart(
                                tool_name="memory_propose_update",
                                tool_call_id="tool-3",
                                content='{"status":"approved_pending_apply","reason":"","requires_user_confirmation":false}',
                            )
                        ]
                    ),
                ],
                None,
            )
        )
        memory_writer = MagicMock()
        orch = Orchestrator(
            store=mock_store,
            config=_runtime_config(),
            idempotency=mock_idempotency,
            memory_writer=memory_writer,
            pydantic_ai_adapter=mock_pydantic_adapter,
        )

        with pytest.raises(RuntimeError, match="persist failed"):
            await orch.execute_turn(_minimal_event(text="remember this"))
        memory_writer.apply_intent.assert_not_called()
