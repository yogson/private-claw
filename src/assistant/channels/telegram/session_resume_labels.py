"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Session label and preview extraction utilities for Telegram resume menus.
"""

from assistant.store.models import SessionRecord, SessionRecordType

MAX_PREVIEW_LENGTH = 100
MAX_LABEL_LENGTH = 40


def extract_label(records: list[SessionRecord]) -> str:
    """Build a user-facing label from summary, first user message, or session id."""
    for record in records:
        if record.record_type == SessionRecordType.TURN_SUMMARY:
            text = str(record.payload.get("summary_text", ""))
            if text:
                return text[:MAX_LABEL_LENGTH]
    for record in records:
        if record.record_type == SessionRecordType.USER_MESSAGE:
            content = str(record.payload.get("content", ""))
            if content:
                return content[:MAX_LABEL_LENGTH]
    return str(records[0].session_id)[:MAX_LABEL_LENGTH]


def extract_preview(records: list[SessionRecord]) -> str:
    """Build a short preview snippet from the latest user/assistant message."""
    for record in reversed(records):
        if record.record_type in (
            SessionRecordType.USER_MESSAGE,
            SessionRecordType.ASSISTANT_MESSAGE,
        ):
            content = str(record.payload.get("content", ""))
            if content:
                return content[:MAX_PREVIEW_LENGTH]
    return ""
