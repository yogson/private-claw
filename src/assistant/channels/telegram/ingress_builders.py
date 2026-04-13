"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram ingress helper builders for normalized events and attachment metadata.
"""

import mimetypes
from datetime import UTC, datetime
from typing import Any

from assistant.channels.telegram.models import (
    AttachmentMeta,
    CallbackQueryMeta,
    EventSource,
    EventType,
    NormalizedEvent,
    VoiceMeta,
)

_FALLBACK_MIME_TYPE = "application/octet-stream"
_VOICE_MISSING_TRANSCRIPT = (
    "I could not extract voice text from Telegram. "
    "Please resend as text or try another voice message."
)


def build_text_event(
    message: dict[str, Any],
    user_id: int,
    session_id: str,
    event_id: str,
    trace_id: str,
    created_at: datetime,
) -> NormalizedEvent:
    """Build a normalized text-message event from Telegram message payload."""
    chat_id = message.get("chat", {}).get("id", user_id)
    text = message.get("text") or message.get("caption", "")
    return NormalizedEvent(
        event_id=event_id,
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id=str(user_id),
        created_at=created_at,
        trace_id=trace_id,
        text=text or None,
        idempotency_key=f"telegram:{message.get('message_id', event_id)}",
        metadata={"chat_id": chat_id, "message_id": message.get("message_id")},
    )


def build_voice_event(
    message: dict[str, Any],
    user_id: int,
    session_id: str,
    event_id: str,
    trace_id: str,
    created_at: datetime,
) -> NormalizedEvent:
    """Build a normalized voice-message event with inline transcript fallback."""
    voice_data = message["voice"]
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
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id=str(user_id),
        created_at=created_at,
        trace_id=trace_id,
        text=text,
        voice=voice_meta,
        idempotency_key=f"telegram:{message.get('message_id', event_id)}",
        metadata={"chat_id": message.get("chat", {}).get("id", user_id)},
    )


def build_attachment_event(
    message: dict[str, Any],
    user_id: int,
    session_id: str,
    event_id: str,
    trace_id: str,
    created_at: datetime,
) -> NormalizedEvent:
    """Build a normalized attachment-message event from document/photo payload."""
    caption = message.get("caption")
    attachment_meta = extract_attachment_meta(message)
    return NormalizedEvent(
        event_id=event_id,
        event_type=EventType.USER_ATTACHMENT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id=str(user_id),
        created_at=created_at,
        trace_id=trace_id,
        text=caption,
        attachment=attachment_meta,
        idempotency_key=f"telegram:{message.get('message_id', event_id)}",
        metadata={"chat_id": message.get("chat", {}).get("id", user_id)},
    )


def build_media_group_event(
    messages: list[dict[str, Any]],
    user_id: int,
    session_id: str,
    event_id: str,
    trace_id: str,
    created_at: datetime,
) -> NormalizedEvent:
    """Build a single normalized event from all messages in a Telegram media group.

    Each message in the album contributes one AttachmentMeta entry.  The
    caption from the first message that carries one is used as the event
    text.  All attachments are placed in both ``attachment`` (the primary
    field, for backwards compatibility) and ``attachments`` (the full list).
    """
    attachments: list[AttachmentMeta] = []
    caption: str | None = None
    for msg in messages:
        if has_document_or_photo(msg):
            attachments.append(extract_attachment_meta(msg))
        if caption is None:
            caption = msg.get("caption") or None

    first = messages[0]
    chat_id = first.get("chat", {}).get("id", user_id)
    media_group_id: str = first.get("media_group_id") or event_id

    return NormalizedEvent(
        event_id=event_id,
        event_type=EventType.USER_ATTACHMENT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id=str(user_id),
        created_at=created_at,
        trace_id=trace_id,
        text=caption,
        attachment=attachments[0] if attachments else None,
        attachments=attachments,
        idempotency_key=f"telegram:mg:{media_group_id}",
        metadata={"chat_id": chat_id, "media_group_id": media_group_id},
    )


def build_web_app_data_event(
    message: dict[str, Any],
    user_id: int,
    session_id: str,
    event_id: str,
    trace_id: str,
    created_at: datetime,
) -> NormalizedEvent:
    """Build a normalized text event from a Telegram WebApp data submission."""
    chat_id = message.get("chat", {}).get("id", user_id)
    web_app_data = message.get("web_app_data", {})
    data_text: str = web_app_data.get("data", "")
    return NormalizedEvent(
        event_id=event_id,
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id=str(user_id),
        created_at=created_at,
        trace_id=trace_id,
        text=data_text or None,
        idempotency_key=f"telegram:{message.get('message_id', event_id)}",
        metadata={"chat_id": chat_id, "message_id": message.get("message_id")},
    )


def build_callback_query_event(
    cq: dict[str, Any], user_id: int, event_id: str, trace_id: str
) -> NormalizedEvent:
    """Build a normalized callback-query event for inline keyboard interactions."""
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
        source=EventSource.TELEGRAM,
        session_id=session_id,
        user_id=str(user_id),
        created_at=datetime.now(UTC),
        trace_id=trace_id,
        callback_query=callback_query_meta,
        idempotency_key=f"telegram:cq:{cq.get('id', event_id)}",
        metadata={"chat_id": chat_id},
    )


def extract_attachment_meta(message: dict[str, Any]) -> AttachmentMeta:
    """Extract attachment metadata from Telegram document or photo fields."""
    if "document" in message:
        doc = message["document"]
        file_name = doc.get("file_name")
        raw_mime = doc.get("mime_type")
        mime_type = normalize_document_mime_type(raw_mime, file_name)
        return AttachmentMeta(
            file_id=doc.get("file_id", ""),
            mime_type=mime_type,
            file_size_bytes=doc.get("file_size", 0),
            file_name=file_name,
            caption=message.get("caption"),
        )
    if "photo" in message:
        photos: list[dict[str, Any]] = message["photo"]
        largest = max(photos, key=lambda photo: photo.get("file_size", 0), default={})
        return AttachmentMeta(
            file_id=largest.get("file_id", ""),
            mime_type="image/jpeg",
            file_size_bytes=largest.get("file_size", 0),
            caption=message.get("caption"),
        )
    return AttachmentMeta(file_id="", mime_type=_FALLBACK_MIME_TYPE, file_size_bytes=0)


def normalize_document_mime_type(raw_mime: str | None, file_name: str | None) -> str:
    """Return resolved MIME type, inferring from filename for octet-stream docs."""
    if raw_mime and raw_mime != _FALLBACK_MIME_TYPE:
        return raw_mime
    if file_name:
        guessed, _ = mimetypes.guess_type(file_name)
        if guessed:
            return guessed
    return raw_mime or _FALLBACK_MIME_TYPE


def has_document_or_photo(message: dict[str, Any]) -> bool:
    """Return True when message contains a document or photo payload."""
    return "document" in message or "photo" in message


def extract_user_id(message: dict[str, Any]) -> int | None:
    """Extract sender user id from message payload, or None when absent."""
    from_user = message.get("from") or {}
    uid = from_user.get("id")
    return int(uid) if uid is not None else None


def parse_date(ts: int | None) -> datetime:
    """Parse Telegram unix timestamp into UTC datetime, defaulting to now."""
    if ts is not None:
        return datetime.fromtimestamp(ts, tz=UTC)
    return datetime.now(UTC)
