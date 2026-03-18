"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Orchestrator service executing turn lifecycle with persistence and idempotency.
"""

from datetime import UTC, datetime
from typing import Any

import structlog

from assistant.agent.interfaces import LLMMessage, MessageRole
from assistant.agent.pydantic_ai_agent import (
    PydanticAITurnAdapter,
    TurnDeps,
    _extract_pending_ask_question,
    _new_messages_to_plans,
    _new_messages_to_session_records,
)
from assistant.agent.tools import build_tool_runtime_params
from assistant.core.config.schemas import RuntimeConfig
from assistant.core.events.models import AttachmentMeta, OrchestratorEvent
from assistant.core.orchestrator.attachments import AttachmentDownloaderInterface
from assistant.core.orchestrator.memory import apply_approved_memory_intents
from assistant.core.orchestrator.models import OrchestratorResult
from assistant.core.orchestrator.payloads import (
    _PLACEHOLDER_EMPTY,
    build_user_content_blocks,
    extract_raw_text_for_multimodal,
    extract_user_text,
    format_attachment_context,
    gather_attachments,
    records_to_messages,
)
from assistant.core.orchestrator.persistence import (
    persist_turn_failed,
    persist_turn_initial,
    persist_turn_outcomes,
    persist_turn_terminal_failed,
)
from assistant.memory.interfaces import MemoryRetrievalInterface, MemoryWriterInterface
from assistant.memory.retrieval.models import RetrievalQuery
from assistant.memory.store.models import MemoryType
from assistant.observability.correlation import (
    SessionTraceManager,
    reset_trace_id,
    set_trace_id,
)
from assistant.store.idempotency.service import IngressIdempotencyService
from assistant.store.interfaces import LockAcquisitionError, StoreFacadeInterface
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
)
from assistant.subagents.interfaces import DelegationCoordinatorInterface

logger = structlog.get_logger(__name__)

_REPLAY_BUDGET = 50
_LONG_HISTORY_THRESHOLD = 20
_SYSTEM_REMINDER = (
    "[System reminder: Follow your delegation instructions. "
    "Delegate all coding tasks (develop, review, test, debug) to sub-agents. "
    "If delegation fails, ask the user what to do next—do not complete the task yourself.]"
)
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
        memory_writer: MemoryWriterInterface | None = None,
        memory_retrieval: MemoryRetrievalInterface | None = None,
        pydantic_ai_adapter: PydanticAITurnAdapter | None = None,
        delegation_coordinator: DelegationCoordinatorInterface | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._idempotency = idempotency
        self._attachment_downloader = attachment_downloader
        self._memory_writer = memory_writer
        self._memory_retrieval = memory_retrieval
        self._pydantic_ai_adapter = pydantic_ai_adapter
        self._delegation_coordinator = delegation_coordinator
        self._session_traces = SessionTraceManager()

    async def execute_turn(self, event: OrchestratorEvent) -> OrchestratorResult | None:
        """
        Execute one turn for the given event.

        Returns OrchestratorResult on success, None if duplicate (caller
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
        current_user_message: LLMMessage,
        user_content: str,
        attachments: list[AttachmentMeta],
        session_id: str,
        turn_id: str,
        trace_id: str,
        user_id: str | None = None,
        model_id_override: str | None = None,
    ) -> OrchestratorResult:
        """Execute a turn via the configured Pydantic AI adapter."""
        adapter = self._pydantic_ai_adapter
        if adapter is None:
            raise RuntimeError("Pydantic AI adapter not configured")

        model_id = model_id_override or self._config.model.default_model_id
        if model_id not in self._config.model.model_allowlist:
            model_id = self._config.model.default_model_id

        msg_dicts = [
            {"role": m.role.value, "content": m.content, "content_blocks": m.content_blocks}
            for m in messages
        ]
        tool_params = build_tool_runtime_params(self._config)
        memory_handler = None
        if self._memory_retrieval:

            def _handler(q: str, limit: int, mt: list[str] | None) -> dict[str, Any]:
                return self._memory_search(q, limit, mt, user_id=user_id)

            memory_handler = _handler
        delegation_handler = None
        if self._delegation_coordinator is not None:
            coordinator = self._delegation_coordinator

            async def delegation_handler(payload: dict[str, Any]) -> dict[str, Any]:
                try:
                    import logfire

                    ctx = logfire.get_context()
                    if ctx:
                        payload["logfire_context"] = dict(ctx)
                except Exception:
                    pass
                return await coordinator.enqueue_from_tool(
                    session_id=session_id,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    user_id=user_id,
                    request=payload,
                )

        deps = TurnDeps(
            writes_approved=[],
            seen_intent_ids=set(),
            memory_search_handler=memory_handler,
            delegation_enqueue_handler=delegation_handler,
            tool_runtime_params=tool_params,
        )
        response_text, new_msgs, usage = await adapter.run_turn(
            messages=msg_dicts,
            deps=deps,
            trace_id=trace_id,
            model_id=model_id,
        )
        prompt_trace: dict[str, Any] | None = None
        if self._config.model.prompt_trace_enabled:
            prompt_trace = {
                "system_prompt": adapter.system_prompt,
                "user_prompt": {
                    "content": current_user_message.content,
                    "content_blocks": current_user_message.content_blocks,
                },
            }
        memory_plans = _new_messages_to_plans(new_msgs)
        now = datetime.now(UTC)
        assistant_msg_id = f"msg-{turn_id}-assistant"
        assistant_records = _new_messages_to_session_records(
            new_msgs,
            session_id=session_id,
            turn_id=turn_id,
            timestamp=now,
            assistant_msg_id=assistant_msg_id,
            model_id=model_id,
            usage=usage,
            user_id=user_id,
            skip_memory_tool_results=True,
        )
        if not assistant_records:
            fallback_payload: dict[str, Any] = {
                "message_id": assistant_msg_id,
                "content": response_text,
                "model_id": model_id,
            }
            if usage is not None:
                fallback_payload["usage"] = usage
            if user_id is not None:
                fallback_payload["user_id"] = user_id
            assistant_records = [
                SessionRecord(
                    session_id=session_id,
                    sequence=0,
                    event_id=assistant_msg_id,
                    turn_id=turn_id,
                    timestamp=now,
                    record_type=SessionRecordType.ASSISTANT_MESSAGE,
                    payload=fallback_payload,
                )
            ]
        pending_ask = _extract_pending_ask_question(
            new_msgs, session_id=session_id, turn_id=turn_id
        )
        initial_persisted = False
        try:
            await persist_turn_initial(
                self._store.sessions,
                self._config,
                session_id=session_id,
                turn_id=turn_id,
                user_text=user_content,
                assistant_records=assistant_records,
                attachments=[a.model_dump() for a in attachments],
                invalid_memory_intents=0,
                prompt_trace=prompt_trace,
                user_id=user_id,
            )
            initial_persisted = True
            outcomes = apply_approved_memory_intents(
                memory_plans, self._memory_writer, user_id=user_id
            )
            await persist_turn_outcomes(
                self._store.sessions,
                session_id=session_id,
                turn_id=turn_id,
                outcomes=outcomes,
            )
            return OrchestratorResult(text=response_text, pending_ask=pending_ask)
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
                    user_id=user_id,
                )
            raise

    async def _run_turn(self, event: OrchestratorEvent) -> OrchestratorResult:
        user_text = extract_user_text(event)
        attachments = gather_attachments(event)
        attachment_context = format_attachment_context(attachments)
        user_content = (user_text + attachment_context).strip() or _PLACEHOLDER_EMPTY

        turn_id = event.event_id
        session_id = event.session_id
        trace_id = event.trace_id

        raw_text = extract_raw_text_for_multimodal(event)
        content_blocks = await build_user_content_blocks(
            raw_text, attachments, self._attachment_downloader, trace_id
        )
        if content_blocks is not None:
            user_message = LLMMessage(
                role=MessageRole.USER, content="", content_blocks=content_blocks
            )
        else:
            user_message = LLMMessage(role=MessageRole.USER, content=user_content)

        token = set_trace_id(trace_id)
        try:
            with self._session_traces.session_trace(session_id, turn_id):
                records = await self._store.sessions.replay_for_turn(session_id, _REPLAY_BUDGET)
                messages = records_to_messages(records)
                if len(messages) > _LONG_HISTORY_THRESHOLD:
                    reminder = _SYSTEM_REMINDER + "\n\n"
                    if content_blocks is not None:
                        user_message = LLMMessage(
                            role=MessageRole.USER,
                            content="",
                            content_blocks=[{"type": "text", "text": reminder}]
                            + list(content_blocks),
                        )
                    else:
                        user_message = LLMMessage(
                            role=MessageRole.USER, content=reminder + user_content
                        )
                messages.append(user_message)

                return await self._run_turn_pydantic_ai(
                    messages=messages,
                    current_user_message=user_message,
                    user_content=user_content,
                    attachments=attachments,
                    session_id=session_id,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    user_id=event.user_id,
                    model_id_override=event.model_id_override,
                )
        finally:
            reset_trace_id(token)

    def _memory_search(
        self,
        query: str,
        limit: int,
        memory_types: list[str] | None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if self._memory_retrieval is None:
            return {
                "status": "unavailable",
                "reason": "memory retrieval unavailable",
                "matches": [],
            }
        intent_types: list[MemoryType] = []
        for raw in memory_types or []:
            try:
                intent_types.append(MemoryType(raw))
            except ValueError:
                continue
        retrieval_query = RetrievalQuery(
            user_id=user_id,
            user_query_text=query.strip() if query else "",
            intent_types=intent_types,
        )
        retrieval_result = self._memory_retrieval.retrieve(retrieval_query)
        bounded_limit = max(1, min(limit, 5))
        matches: list[dict[str, Any]] = []
        for scored in retrieval_result.scored_artifacts[:bounded_limit]:
            artifact = scored.artifact
            body = artifact.body.strip()
            if len(body) > 500:
                body = body[:500] + "... [truncated]"
            matches.append({"body": body})
        return {
            "status": "ok",
            "query": query,
            "matches": matches,
        }
