"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Orchestrator service executing turn lifecycle with persistence and idempotency.
"""

from datetime import UTC, datetime
from typing import Any

import structlog

from assistant.core.attachments import AttachmentDownloaderInterface
from assistant.core.config.schemas import RuntimeConfig
from assistant.core.events.models import OrchestratorEvent
from assistant.core.orchestrator_payloads import (
    _PLACEHOLDER_EMPTY,
    build_user_content_blocks,
    extract_raw_text_for_multimodal,
    extract_user_text,
    format_attachment_context,
    gather_attachments,
    records_to_messages,
)
from assistant.observability.correlation import reset_trace_id, set_trace_id
from assistant.providers.interfaces import LLMMessage, LLMProviderInterface, LLMRequest, MessageRole
from assistant.store.idempotency.service import IngressIdempotencyService
from assistant.store.interfaces import LockAcquisitionError, StoreFacadeInterface
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    TurnTerminalStatus,
    UserMessagePayload,
)

logger = structlog.get_logger(__name__)

_REPLAY_BUDGET = 50
_LOCK_KEY_PREFIX = "session:"
_LOCK_OWNER_PREFIX = "orchestrator:"


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
        attachment_downloader: AttachmentDownloaderInterface | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._config = config
        self._idempotency = idempotency
        self._attachment_downloader = attachment_downloader

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
            records = await self._store.sessions.replay_for_turn(session_id, _REPLAY_BUDGET)
            messages = records_to_messages(records)
            messages.append(user_message)
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
                user_text=user_content,
                assistant_text=response_text,
                trace_id=trace_id,
                attachments=[a.model_dump() for a in attachments],
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
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        next_seq = await self._store.sessions.get_next_sequence(session_id)

        user_msg_id = f"msg-{turn_id}-user"
        assistant_msg_id = f"msg-{turn_id}-assistant"
        user_payload = UserMessagePayload(
            message_id=user_msg_id,
            content=user_text,
            attachments=attachments or [],
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
                payload={"status": TurnTerminalStatus.COMPLETED.value},
            ),
        ]
        await self._store.sessions.append(records)
