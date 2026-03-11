"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Minimal orchestrator for turn-based event handling. Executes direct model path
with session replay, idempotency, and lock-protected persistence.
"""

import base64
from datetime import UTC, datetime
from typing import Any

import structlog

from assistant.core.attachments import AttachmentDownloaderInterface
from assistant.core.config.schemas import RuntimeConfig
from assistant.core.events.models import AttachmentMeta, OrchestratorEvent
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

_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_PDF_MIME_TYPE = "application/pdf"


_PLACEHOLDER_EMPTY = "[Empty or unsupported input]"


def _extract_user_text(event: OrchestratorEvent) -> str:
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
    return _PLACEHOLDER_EMPTY


def _extract_raw_text_for_multimodal(event: OrchestratorEvent) -> str | None:
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
    return None


def _gather_attachments(event: OrchestratorEvent) -> list[AttachmentMeta]:
    out: list[AttachmentMeta] = []
    if event.attachment and event.attachment.file_id:
        out.append(event.attachment)
    for att in event.attachments or []:
        if att.file_id:
            out.append(att)
    return out


def _format_attachment_context(attachments: list[AttachmentMeta]) -> str:
    if not attachments:
        return ""
    parts = []
    for att in attachments:
        size_str = f"{att.file_size_bytes / 1024:.1f} KB" if att.file_size_bytes else "unknown size"
        media_type = "image" if att.mime_type.startswith("image/") else "file"
        parts.append(f"{media_type} ({att.mime_type}, {size_str})")
    return "\n\n[User attached: " + "; ".join(parts) + "]"


def _is_multimodal_supported(mime_type: str) -> bool:
    return mime_type in _IMAGE_MIME_TYPES or mime_type == _PDF_MIME_TYPE


async def _build_user_content_blocks(
    raw_text: str | None,
    attachments: list[AttachmentMeta],
    downloader: AttachmentDownloaderInterface | None,
    trace_id: str,
) -> list[dict[str, Any]] | None:
    if not downloader or not attachments:
        return None

    blocks: list[dict[str, Any]] = []
    if raw_text and raw_text.strip() and raw_text != _PLACEHOLDER_EMPTY:
        blocks.append({"type": "text", "text": raw_text.strip()})

    for att in attachments:
        if not _is_multimodal_supported(att.mime_type):
            continue
        try:
            data = await downloader.download(
                att.file_id, att.mime_type, att.file_size_bytes, trace_id
            )
        except Exception as exc:
            logger.warning(
                "orchestrator.attachment_download_error",
                file_id=att.file_id,
                error=str(exc),
                trace_id=trace_id,
            )
            continue
        if data is None:
            continue
        b64 = base64.standard_b64encode(data).decode("utf-8")
        if att.mime_type in _IMAGE_MIME_TYPES:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime_type,
                        "data": b64,
                    },
                }
            )
        elif att.mime_type == _PDF_MIME_TYPE:
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    },
                }
            )

    has_media = any(b.get("type") in ("image", "document") for b in blocks)
    if not has_media or not blocks:
        return None
    return blocks


def _records_to_messages(records: list[SessionRecord]) -> list[LLMMessage]:
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
        user_text = _extract_user_text(event)
        attachments = _gather_attachments(event)
        attachment_context = _format_attachment_context(attachments)
        user_content = (user_text + attachment_context).strip() or _PLACEHOLDER_EMPTY

        turn_id = event.event_id
        session_id = event.session_id
        trace_id = event.trace_id

        raw_text = _extract_raw_text_for_multimodal(event)
        content_blocks = await _build_user_content_blocks(
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
            messages = _records_to_messages(records)

            is_new_session = not await self._store.sessions.session_exists(session_id)
            if is_new_session and not messages:
                response_text = _DEFAULT_GREETING
                logger.info("orchestrator.greeting", session_id=session_id)
            else:
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
                payload={
                    "status": TurnTerminalStatus.COMPLETED.value,
                },
            ),
        ]
        await self._store.sessions.append(records)
