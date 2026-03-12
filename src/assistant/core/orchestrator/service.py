"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Orchestrator service executing turn lifecycle with persistence and idempotency.
"""

import structlog

from assistant.core.config.schemas import RuntimeConfig
from assistant.core.events.models import AttachmentMeta, OrchestratorEvent
from assistant.core.orchestrator.attachments import AttachmentDownloaderInterface
from assistant.core.orchestrator.memory import apply_approved_memory_intents
from assistant.core.orchestrator.payloads import (
    _PLACEHOLDER_EMPTY,
    build_user_content_blocks,
    extract_raw_text_for_multimodal,
    extract_user_text,
    format_attachment_context,
    format_retrieved_memory_context,
    gather_attachments,
    records_to_messages,
)
from assistant.core.orchestrator.persistence import (
    persist_turn_failed,
    persist_turn_initial,
    persist_turn_outcomes,
    persist_turn_terminal_failed,
)
from assistant.memory.retrieval.models import RetrievalQuery
from assistant.memory.retrieval.service import RetrievalService
from assistant.memory.write.service import MemoryWriteService
from assistant.observability.correlation import reset_trace_id, set_trace_id
from assistant.providers.interfaces import LLMMessage, MessageRole
from assistant.providers.pydantic_ai_agent import (
    PydanticAITurnAdapter,
    TurnDeps,
    _new_messages_to_plans,
)
from assistant.store.idempotency.service import IngressIdempotencyService
from assistant.store.interfaces import LockAcquisitionError, StoreFacadeInterface

logger = structlog.get_logger(__name__)

_REPLAY_BUDGET = 50
_LOCK_KEY_PREFIX = "session:"
_LOCK_OWNER_PREFIX = "orchestrator:"


class Orchestrator:
    """
    Turn-based orchestrator executing via Pydantic AI turn adapter.

    Handles idempotency, session lock, replay assembly, LLM completion,
    and atomic persistence of turn artifacts.
    """

    def __init__(
        self,
        store: StoreFacadeInterface,
        config: RuntimeConfig,
        idempotency: IngressIdempotencyService,
        attachment_downloader: AttachmentDownloaderInterface | None = None,
        memory_writer: MemoryWriteService | None = None,
        memory_retrieval: RetrievalService | None = None,
        pydantic_ai_adapter: PydanticAITurnAdapter | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._idempotency = idempotency
        self._attachment_downloader = attachment_downloader
        self._memory_writer = memory_writer
        self._memory_retrieval = memory_retrieval
        self._pydantic_ai_adapter = pydantic_ai_adapter

    async def execute_turn(self, event: OrchestratorEvent) -> str | None:
        """
        Execute one turn for the given event.

        Returns assistant response text on success, None if duplicate (caller
        should not send a response). Raises on lock timeout or provider failure.
        """
        source = event.source.value
        key = self._idempotency.build_key(source, event.event_id)
        is_dup, _ = await self._idempotency.check_and_register(
            source, event.event_id, ttl_seconds=self._config.store.idempotency_retention_seconds
        )
        if is_dup:
            logger.info("orchestrator.duplicate_ignored", event_id=event.event_id, key=key)
            return None

        lock_key = f"{_LOCK_KEY_PREFIX}{event.session_id}"
        owner = f"{_LOCK_OWNER_PREFIX}{event.trace_id}"
        try:
            async with self._store.locks.lock(
                lock_key, owner, ttl_seconds=self._config.store.lock_ttl_seconds
            ):
                return await self._run_turn(event)
        except LockAcquisitionError as exc:
            logger.warning(
                "orchestrator.lock_timeout",
                session_id=event.session_id,
                trace_id=event.trace_id,
                error=str(exc),
            )
            raise

    async def _run_turn_pydantic_ai(
        self,
        *,
        messages: list[LLMMessage],
        user_content: str,
        attachments: list[AttachmentMeta],
        session_id: str,
        turn_id: str,
        trace_id: str,
    ) -> str:
        """Execute a turn via the configured Pydantic AI adapter."""
        adapter = self._pydantic_ai_adapter
        if adapter is None:
            raise RuntimeError("Pydantic AI adapter not configured")

        msg_dicts = [
            {"role": m.role.value, "content": m.content, "content_blocks": m.content_blocks}
            for m in messages
        ]
        deps = TurnDeps(
            writes_approved=[],
            seen_intent_ids=set(),
        )
        response_text, new_msgs, _usage = await adapter.run_turn(
            messages=msg_dicts,
            deps=deps,
            trace_id=trace_id,
        )
        memory_plans = _new_messages_to_plans(new_msgs)
        initial_persisted = False
        try:
            await persist_turn_initial(
                self._store.sessions,
                self._config,
                session_id=session_id,
                turn_id=turn_id,
                user_text=user_content,
                assistant_text=response_text,
                attachments=[a.model_dump() for a in attachments],
                memory_plans=memory_plans,
                invalid_memory_intents=0,
            )
            initial_persisted = True
            outcomes = apply_approved_memory_intents(memory_plans, self._memory_writer)
            await persist_turn_outcomes(
                self._store.sessions,
                session_id=session_id,
                turn_id=turn_id,
                outcomes=outcomes,
            )
            return response_text
        except Exception:
            if initial_persisted:
                await persist_turn_terminal_failed(
                    self._store.sessions,
                    session_id=session_id,
                    turn_id=turn_id,
                )
            else:
                await persist_turn_failed(
                    self._store.sessions,
                    self._config,
                    session_id=session_id,
                    turn_id=turn_id,
                    user_text=user_content,
                )
            raise

    async def _run_turn(self, event: OrchestratorEvent) -> str:
        user_text = extract_user_text(event)
        attachments = gather_attachments(event)
        attachment_context = format_attachment_context(attachments)
        user_content = (user_text + attachment_context).strip() or _PLACEHOLDER_EMPTY

        turn_id = event.event_id
        session_id = event.session_id
        trace_id = event.trace_id

        memory_context = ""
        if self._memory_retrieval is not None and user_text != _PLACEHOLDER_EMPTY:
            retrieval_query = RetrievalQuery(user_query_text=user_text)
            retrieval_result = self._memory_retrieval.retrieve(retrieval_query)
            memory_context = format_retrieved_memory_context(retrieval_result)

        raw_text = extract_raw_text_for_multimodal(event)
        content_blocks = await build_user_content_blocks(
            raw_text, attachments, self._attachment_downloader, trace_id
        )
        if content_blocks is not None:
            if memory_context:
                content_blocks.insert(0, {"type": "text", "text": memory_context})
            user_message = LLMMessage(
                role=MessageRole.USER, content="", content_blocks=content_blocks
            )
        else:
            content = f"{memory_context}\n\n{user_content}" if memory_context else user_content
            user_message = LLMMessage(role=MessageRole.USER, content=content)

        token = set_trace_id(trace_id)
        try:
            records = await self._store.sessions.replay_for_turn(session_id, _REPLAY_BUDGET)
            messages = records_to_messages(records)
            messages.append(user_message)

            return await self._run_turn_pydantic_ai(
                messages=messages,
                user_content=user_content,
                attachments=attachments,
                session_id=session_id,
                turn_id=turn_id,
                trace_id=trace_id,
            )
        finally:
            reset_trace_id(token)
