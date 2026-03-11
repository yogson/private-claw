"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Minimal orchestrator for turn-based event handling. Executes direct model path
with session replay, idempotency, and lock-protected persistence.
"""

from datetime import UTC, datetime

import structlog

from assistant.core.config.schemas import RuntimeConfig
from assistant.core.events.models import OrchestratorEvent
from assistant.observability.correlation import reset_trace_id, set_trace_id
from assistant.providers.interfaces import (
    LLMMessage,
    LLMProviderInterface,
    LLMRequest,
    MessageRole,
)
from assistant.store.idempotency.service import IngressIdempotencyService
from assistant.store.interfaces import LockAcquisitionError, StoreFacadeInterface
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    TurnTerminalStatus,
    UserMessagePayload,
)

logger = structlog.get_logger(__name__)

_DEFAULT_GREETING = "Hello! How can I help you today?"
_REPLAY_BUDGET = 50
_LOCK_KEY_PREFIX = "session:"
_LOCK_OWNER_PREFIX = "orchestrator:"


def _extract_user_text(event: OrchestratorEvent) -> str:
    """Extract user-facing text from an orchestrator event."""
    if event.text and event.text.strip():
        return event.text.strip()
    if event.voice and event.voice.transcript_text:
        return event.voice.transcript_text.strip()
    if event.attachment and event.attachment.caption:
        return event.attachment.caption.strip()
    if event.attachments:
        for att in event.attachments:
            if att.caption:
                return att.caption.strip()
    if event.callback_query:
        return f"[Callback: {event.callback_query.callback_data[:100]}]"
    return "[Empty or unsupported input]"


def _records_to_messages(records: list[SessionRecord]) -> list[LLMMessage]:
    """Convert session replay records to LLM message format."""
    messages: list[LLMMessage] = []
    for r in records:
        if r.record_type == SessionRecordType.USER_MESSAGE:
            content = r.payload.get("content", "")
            if content:
                messages.append(LLMMessage(role=MessageRole.USER, content=content))
        elif r.record_type == SessionRecordType.ASSISTANT_MESSAGE:
            content = r.payload.get("content", "")
            if content:
                messages.append(LLMMessage(role=MessageRole.ASSISTANT, content=content))
    return messages


class Orchestrator:
    """
    Turn-based orchestrator executing direct model path.

    Handles idempotency, session lock, replay assembly, LLM completion,
    and atomic persistence of turn artifacts.
    """

    def __init__(
        self,
        store: StoreFacadeInterface,
        provider: LLMProviderInterface,
        config: RuntimeConfig,
        idempotency: IngressIdempotencyService,
    ) -> None:
        self._store = store
        self._provider = provider
        self._config = config
        self._idempotency = idempotency

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

    async def _run_turn(self, event: OrchestratorEvent) -> str:
        user_text = _extract_user_text(event)
        turn_id = event.event_id
        session_id = event.session_id
        trace_id = event.trace_id

        token = set_trace_id(trace_id)
        try:
            records = await self._store.sessions.replay_for_turn(session_id, _REPLAY_BUDGET)
            messages = _records_to_messages(records)

            is_new_session = not await self._store.sessions.session_exists(session_id)
            if is_new_session and not messages:
                response_text = _DEFAULT_GREETING
                logger.info("orchestrator.greeting", session_id=session_id)
            else:
                messages.append(LLMMessage(role=MessageRole.USER, content=user_text))
                request = LLMRequest(
                    messages=messages,
                    trace_id=trace_id,
                    model_id=self._config.model.default_model_id,
                    max_tokens=self._config.model.max_tokens_default,
                )
                llm_response = await self._provider.complete(request)
                response_text = llm_response.text

            await self._persist_turn(
                session_id=session_id,
                turn_id=turn_id,
                user_text=user_text,
                assistant_text=response_text,
                trace_id=trace_id,
            )

            return response_text
        finally:
            reset_trace_id(token)

    async def _persist_turn(
        self,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        trace_id: str,
    ) -> None:
        """Append user message, assistant message, and turn terminal to session log."""
        now = datetime.now(UTC)
        next_seq = await self._store.sessions.get_next_sequence(session_id)

        user_msg_id = f"msg-{turn_id}-user"
        assistant_msg_id = f"msg-{turn_id}-assistant"

        user_payload = UserMessagePayload(
            message_id=user_msg_id,
            content=user_text,
            source_event_id=turn_id,
        )

        records = [
            SessionRecord(
                session_id=session_id,
                sequence=next_seq,
                event_id=turn_id,
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.USER_MESSAGE,
                payload=user_payload.model_dump(),
            ),
            SessionRecord(
                session_id=session_id,
                sequence=next_seq + 1,
                event_id=assistant_msg_id,
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.ASSISTANT_MESSAGE,
                payload={
                    "message_id": assistant_msg_id,
                    "content": assistant_text,
                    "model_id": self._config.model.default_model_id,
                },
            ),
            SessionRecord(
                session_id=session_id,
                sequence=next_seq + 2,
                event_id=f"terminal-{turn_id}",
                turn_id=turn_id,
                timestamp=now,
                record_type=SessionRecordType.TURN_TERMINAL,
                payload={
                    "status": TurnTerminalStatus.COMPLETED.value,
                },
            ),
        ]
        await self._store.sessions.append(records)
