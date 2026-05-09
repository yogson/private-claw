"""
Component ID: CMP_CORE_CHAT_COMPACTION

Reusable token usage calculation from persisted session records.

Extracted from telegram usage aggregation for cross-component reuse.
"""

from assistant.store.interfaces import SessionStoreInterface
from assistant.store.models import SessionRecord, SessionRecordType


async def calculate_session_total_tokens(
    session_store: SessionStoreInterface,
    session_id: str,
    records: list[SessionRecord] | None = None,
) -> int:
    """Sum all input+output tokens from assistant_message records in a session.

    Args:
        session_store: Session persistence interface.
        session_id: The session to calculate tokens for.
        records: Optional pre-loaded session records to avoid a redundant read.

    Returns:
        Total token count (input + output) across all assistant messages.
    """
    if records is None:
        records = await session_store.read_session(session_id)
    total = 0
    for r in records:
        if r.record_type == SessionRecordType.ASSISTANT_MESSAGE:
            usage = r.payload.get("usage", {})
            if isinstance(usage, dict):
                total += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    return total
