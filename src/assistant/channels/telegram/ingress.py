"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram update normalization to INT_ORCH_EVENT_INPUT contract.
Handles text messages and callback_query updates.
Voice and attachment metadata extraction is handled in subsequent ingestion modules.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from assistant.channels.telegram.allowlist import AllowlistGuard
from assistant.channels.telegram.models import (
    AttachmentMeta,
    CallbackQueryMeta,
    EventType,
    NormalizedEvent,
    VoiceMeta,
)
from assistant.observability.correlation import get_trace_id as _get_trace_id

logger = structlog.get_logger(__name__)

_VOICE_MISSING_TRANSCRIPT = (
    "I could not extract voice text from Telegram. "
    "Please resend as text or try another voice message."
)


class TelegramIngress:
    """
    Converts raw Telegram update dicts into NormalizedEvent objects.

    Enforces allowlist before normalization. Unknown or unsupported update
    types are silently dropped and logged.
    """

    def __init__(self, guard: AllowlistGuard) -> None:
        self._guard = guard

    def normalize(self, update: dict[str, Any]) -> NormalizedEvent | None:
        """
        Normalize a Telegram update into a NormalizedEvent.

        Returns None if the update type is unsupported or the user is not allowed.
        The caller must handle UnauthorizedUserError if it propagates.
        """
        if "message" in update:
            return self._normalize_message(update["message"])
        if "callback_query" in update:
            return self._normalize_callback_query(update["callback_query"])
        logger.debug("telegram.ingress.unsupported_update", keys=list(update.keys()))
        return None

    def _normalize_message(self, message: dict[str, Any]) -> NormalizedEvent | None:
        user_id = self._extract_user_id(message)
        if user_id is None:
            return None
        self._guard.require_allowed(user_id)

        chat_id = message.get("chat", {}).get("id", user_id)
        session_id = f"tg:{chat_id}"
        event_id = str(uuid.uuid4())
        trace_id = _get_trace_id() or event_id
        created_at = self._parse_date(message.get("date"))

        if "voice" in message:
            return self._build_voice_event(
                message, user_id, session_id, event_id, trace_id, created_at
            )
        if self._has_document_or_photo(message):
            return self._build_attachment_event(
                message, user_id, session_id, event_id, trace_id, created_at
            )
        text = message.get("text") or message.get("caption", "")
        return NormalizedEvent(
            event_id=event_id,
            event_type=EventType.USER_TEXT_MESSAGE,
            source="telegram",
            session_id=session_id,
            user_id=str(user_id),
            created_at=created_at,
            trace_id=trace_id,
            text=text or None,
            idempotency_key=f"telegram:{message.get('message_id', event_id)}",
            metadata={"chat_id": chat_id, "message_id": message.get("message_id")},
        )

    def _normalize_callback_query(self, cq: dict[str, Any]) -> NormalizedEvent | None:
        from_user = cq.get("from") or {}
        user_id_raw = from_user.get("id")
        if user_id_raw is None:
            return None
        user_id = int(user_id_raw)
        self._guard.require_allowed(user_id)

        event_id = str(uuid.uuid4())
        trace_id = _get_trace_id() or event_id
        message = cq.get("message") or {}
        chat_id = message.get("chat", {}).get("id", user_id)
        session_id = f"tg:{chat_id}"

        callback_query_meta = CallbackQueryMeta(
            callback_id=cq.get("id", event_id),
            callback_data=cq.get("data", ""),
            origin_message_id=message.get("message_id"),
            ui_version="1",
        )
        return NormalizedEvent(
            event_id=event_id,
            event_type=EventType.USER_CALLBACK_QUERY,
            source="telegram",
            session_id=session_id,
            user_id=str(user_id),
            created_at=datetime.now(UTC),
            trace_id=trace_id,
            callback_query=callback_query_meta,
            idempotency_key=f"telegram:cq:{cq.get('id', event_id)}",
            metadata={"chat_id": chat_id},
        )

    def _build_voice_event(
        self,
        message: dict[str, Any],
        user_id: int,
        session_id: str,
        event_id: str,
        trace_id: str,
        created_at: datetime,
    ) -> NormalizedEvent:
        voice_data = message["voice"]
        # Telegram may provide transcript text via the top-level message text field
        # for premium accounts; otherwise None triggers the missing-transcript fallback.
        transcript_text: str | None = message.get("text") or None

        voice_meta = VoiceMeta(
            file_id=voice_data.get("file_id", ""),
            duration_seconds=voice_data.get("duration", 0),
            transcript_text=transcript_text,
        )
        text = transcript_text or _VOICE_MISSING_TRANSCRIPT
        return NormalizedEvent(
            event_id=event_id,
            event_type=EventType.USER_VOICE_MESSAGE,
            source="telegram",
            session_id=session_id,
            user_id=str(user_id),
            created_at=created_at,
            trace_id=trace_id,
            text=text,
            voice=voice_meta,
            idempotency_key=f"telegram:{message.get('message_id', event_id)}",
            metadata={"chat_id": message.get("chat", {}).get("id", user_id)},
        )

    def _build_attachment_event(
        self,
        message: dict[str, Any],
        user_id: int,
        session_id: str,
        event_id: str,
        trace_id: str,
        created_at: datetime,
    ) -> NormalizedEvent:
        caption = message.get("caption")
        attachment_meta = self._extract_attachment_meta(message)
        return NormalizedEvent(
            event_id=event_id,
            event_type=EventType.USER_ATTACHMENT_MESSAGE,
            source="telegram",
            session_id=session_id,
            user_id=str(user_id),
            created_at=created_at,
            trace_id=trace_id,
            text=caption,
            attachment=attachment_meta,
            idempotency_key=f"telegram:{message.get('message_id', event_id)}",
            metadata={"chat_id": message.get("chat", {}).get("id", user_id)},
        )

    @staticmethod
    def _extract_attachment_meta(message: dict[str, Any]) -> AttachmentMeta:
        if "document" in message:
            doc = message["document"]
            return AttachmentMeta(
                file_id=doc.get("file_id", ""),
                mime_type=doc.get("mime_type", "application/octet-stream"),
                file_size_bytes=doc.get("file_size", 0),
                caption=message.get("caption"),
            )
        if "photo" in message:
            photos: list[dict[str, Any]] = message["photo"]
            largest = max(photos, key=lambda p: p.get("file_size", 0), default={})
            return AttachmentMeta(
                file_id=largest.get("file_id", ""),
                mime_type="image/jpeg",
                file_size_bytes=largest.get("file_size", 0),
                caption=message.get("caption"),
            )
        return AttachmentMeta(file_id="", mime_type="application/octet-stream", file_size_bytes=0)

    @staticmethod
    def _has_document_or_photo(message: dict[str, Any]) -> bool:
        return "document" in message or "photo" in message

    @staticmethod
    def _extract_user_id(message: dict[str, Any]) -> int | None:
        from_user = message.get("from") or {}
        uid = from_user.get("id")
        return int(uid) if uid is not None else None

    @staticmethod
    def _parse_date(ts: int | None) -> datetime:
        if ts is not None:
            return datetime.fromtimestamp(ts, tz=UTC)
        return datetime.now(UTC)
