"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram ingress service for normalizing updates into channel events.
"""

import uuid
from typing import Any

import structlog

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError
from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.channels.telegram.ingress_builders import (
    _VOICE_MISSING_TRANSCRIPT as _BUILDER_VOICE_MISSING_TRANSCRIPT,
)
from assistant.channels.telegram.ingress_builders import (
    build_attachment_event,
    build_callback_query_event,
    build_text_event,
    build_voice_event,
    extract_user_id,
    has_document_or_photo,
    parse_date,
)
from assistant.channels.telegram.models import EventType, NormalizedEvent
from assistant.channels.telegram.reliability.audit import ChannelAuditLogger
from assistant.channels.telegram.reliability.throttle import ChannelThrottleGuard, ThrottledError
from assistant.observability.correlation import get_trace_id as _get_trace_id

logger = structlog.get_logger(__name__)
_VOICE_MISSING_TRANSCRIPT = _BUILDER_VOICE_MISSING_TRANSCRIPT


class TelegramIngress:
    """
    Converts raw Telegram update dicts into NormalizedEvent objects.

    Enforces allowlist before normalization. Unknown or unsupported update
    types are silently dropped and logged.

    Pass a VoiceTranscriptionService to enable MTProto transcription enrichment
    via normalize_async(). Pass ChannelThrottleGuard and ChannelAuditLogger to
    activate per-user rate limiting and structured audit telemetry.
    """

    def __init__(
        self,
        guard: AllowlistGuard,
        transcription_service: VoiceTranscriptionService | None = None,
        throttle_guard: ChannelThrottleGuard | None = None,
        audit_logger: ChannelAuditLogger | None = None,
    ) -> None:
        self._guard = guard
        self._transcription_service = transcription_service
        self._throttle_guard = throttle_guard
        self._audit_logger = audit_logger

    def normalize(self, update: dict[str, Any]) -> NormalizedEvent | None:
        """
        Normalize a Telegram update into a NormalizedEvent.

        Returns None if the update type is unsupported or the user is not allowed.
        The caller must handle UnauthorizedUserError if it propagates.
        Voice events use inline Telegram transcript only; use normalize_async()
        to enrich voice events via the MTProto transcription service.
        """
        if "message" in update:
            return self._normalize_message(update["message"])
        if "callback_query" in update:
            return self._normalize_callback_query(update["callback_query"])
        logger.debug("telegram.ingress.unsupported_update", keys=list(update.keys()))
        return None

    async def normalize_async(self, update: dict[str, Any]) -> NormalizedEvent | None:
        """
        Normalize a Telegram update with async MTProto transcription enrichment.

        For voice messages without an inline transcript, calls the configured
        VoiceTranscriptionService to enrich voice.transcript_text before
        returning the event. On failure, the event proceeds with transcript_text=None
        and failure reason stored in metadata.audit_transcription_failure.
        Falls back to sync normalize() for non-voice updates or when no service
        is configured.
        """
        event = self.normalize(update)
        if (
            event is None
            or event.event_type != EventType.USER_VOICE_MESSAGE
            or event.voice is None
            or event.voice.transcript_text is not None
            or self._transcription_service is None
        ):
            return event

        transcript, failure_reason = await self._transcription_service.transcribe(
            file_id=event.voice.file_id,
            duration_seconds=event.voice.duration_seconds,
            trace_id=event.trace_id,
        )

        if transcript:
            enriched_voice = event.voice.model_copy(update={"transcript_text": transcript})
            return event.model_copy(update={"voice": enriched_voice, "text": transcript})

        if failure_reason:
            updated_metadata = {**event.metadata, "audit_transcription_failure": failure_reason}
            return event.model_copy(update={"metadata": updated_metadata})

        return event

    def _normalize_message(self, message: dict[str, Any]) -> NormalizedEvent | None:
        user_id = extract_user_id(message)
        if user_id is None:
            return None

        pre_trace_id = _get_trace_id() or str(uuid.uuid4())

        try:
            self._guard.require_allowed(user_id)
        except UnauthorizedUserError:
            if self._audit_logger is not None:
                self._audit_logger.log_ingress_blocked(
                    user_id=user_id,
                    reason="not_in_allowlist",
                    trace_id=pre_trace_id,
                )
            raise

        if self._throttle_guard is not None:
            try:
                self._throttle_guard.check(user_id, trace_id=pre_trace_id)
            except ThrottledError as exc:
                if self._audit_logger is not None:
                    self._audit_logger.log_ingress_throttled(
                        user_id=user_id,
                        count=exc.count,
                        limit=exc.limit,
                        trace_id=pre_trace_id,
                    )
                raise

        chat_id = message.get("chat", {}).get("id", user_id)
        session_id = f"tg:{chat_id}"
        event_id = str(uuid.uuid4())
        trace_id = _get_trace_id() or event_id
        created_at = parse_date(message.get("date"))

        if "voice" in message:
            event = build_voice_event(message, user_id, session_id, event_id, trace_id, created_at)
        elif has_document_or_photo(message):
            event = build_attachment_event(
                message, user_id, session_id, event_id, trace_id, created_at
            )
        else:
            event = build_text_event(message, user_id, session_id, event_id, trace_id, created_at)

        if self._audit_logger is not None:
            self._audit_logger.log_ingress_authorized(
                user_id=user_id,
                event_type=event.event_type.value,
                trace_id=event.trace_id,
            )
        return event

    def _normalize_callback_query(self, cq: dict[str, Any]) -> NormalizedEvent | None:
        from_user = cq.get("from") or {}
        user_id_raw = from_user.get("id")
        if user_id_raw is None:
            return None
        user_id = int(user_id_raw)

        pre_trace_id = _get_trace_id() or str(uuid.uuid4())

        try:
            self._guard.require_allowed(user_id)
        except UnauthorizedUserError:
            if self._audit_logger is not None:
                self._audit_logger.log_ingress_blocked(
                    user_id=user_id,
                    reason="not_in_allowlist",
                    trace_id=pre_trace_id,
                )
            raise

        if self._throttle_guard is not None:
            try:
                self._throttle_guard.check(user_id, trace_id=pre_trace_id)
            except ThrottledError as exc:
                if self._audit_logger is not None:
                    self._audit_logger.log_ingress_throttled(
                        user_id=user_id,
                        count=exc.count,
                        limit=exc.limit,
                        trace_id=pre_trace_id,
                    )
                raise

        event_id = str(uuid.uuid4())
        trace_id = _get_trace_id() or event_id
        event = build_callback_query_event(cq, user_id, event_id, trace_id)
        if self._audit_logger is not None:
            self._audit_logger.log_ingress_authorized(
                user_id=user_id,
                event_type=event.event_type.value,
                trace_id=event.trace_id,
            )
        return event
