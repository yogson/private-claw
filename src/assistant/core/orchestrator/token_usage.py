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
    """Return the current context size from the most recent assistant_message record.

    Uses ``usage.last_request_input_tokens`` of the last ``ASSISTANT_MESSAGE`` record
    (highest ``sequence``) that carries a ``usage`` field: the input size (cache
    included) of the final model request of that turn, i.e. the real context.
    ``usage.input_tokens`` is NOT used: it sums all requests of a multi-request
    (tool-using) run and overstates the context, causing false compactions.

    Fallback: records written before ``last_request_input_tokens`` existed, or whose
    provider reported no per-request usage, carry only the run total. Their context
    size is unknown and ``0`` is returned, so compaction is not triggered on an
    inflated number; the next completed turn persists the real value.

    Args:
        session_store: Session persistence interface.
        session_id: The session to calculate tokens for.
        records: Optional pre-loaded session records to avoid a redundant read.

    Returns:
        ``last_request_input_tokens`` from the most recent assistant message with
        usage data, or ``0`` if there is no such record or it lacks the value.
    """
    if records is None:
        records = await session_store.read_session(session_id)

    last_usage: dict[str, object] = {}
    last_sequence = -1
    for r in records:
        if r.record_type == SessionRecordType.ASSISTANT_MESSAGE:
            usage = r.payload.get("usage")
            if isinstance(usage, dict) and r.sequence > last_sequence:
                last_sequence = r.sequence
                last_usage = usage
    context_tokens = last_usage.get("last_request_input_tokens")
    return int(context_tokens) if isinstance(context_tokens, int) else 0
