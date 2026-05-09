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
    """Return the context window size from the most recent assistant_message record.

    Because each record's ``input_tokens`` already reflects the full accumulated
    context (not a per-turn delta), summing across all records would double-count.
    Instead, we look at the ``input_tokens`` of the last ``ASSISTANT_MESSAGE``
    record (highest ``sequence``) that carries a ``usage`` field — this gives the
    true current context size.

    Args:
        session_store: Session persistence interface.
        session_id: The session to calculate tokens for.
        records: Optional pre-loaded session records to avoid a redundant read.

    Returns:
        ``input_tokens`` from the most recent assistant message with usage data,
        or ``0`` if no such record exists.
    """
    if records is None:
        records = await session_store.read_session(session_id)

    last_input_tokens = 0
    last_sequence = -1
    for r in records:
        if r.record_type == SessionRecordType.ASSISTANT_MESSAGE:
            usage = r.payload.get("usage", {})
            if isinstance(usage, dict) and "input_tokens" in usage and r.sequence > last_sequence:
                last_sequence = r.sequence
                last_input_tokens = int(usage["input_tokens"])
    return last_input_tokens
