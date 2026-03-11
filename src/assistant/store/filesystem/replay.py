"""
Component ID: CMP_STORE_SESSION_PERSISTENCE

Session replay logic for model-facing history reconstruction.

Implements the replay contract: reconstruct the ordered, validated subset
of session records suitable for injecting into model context.
"""

from assistant.store.models import SessionRecord, SessionRecordType, SystemMessageScope

_MODEL_TURN_TYPES = frozenset(
    {
        SessionRecordType.USER_MESSAGE,
        SessionRecordType.ASSISTANT_MESSAGE,
        SessionRecordType.ASSISTANT_TOOL_CALL,
        SessionRecordType.TOOL_RESULT,
        SessionRecordType.SYSTEM_MESSAGE,
    }
)


def build_replay(records: list[SessionRecord], budget: int) -> list[SessionRecord]:
    """
    Reconstruct model-facing history from all session records.

    Algorithm:
    - Finds the newest session-scoped system_message and prepends it.
    - Collects only complete turns (turns with a turn_terminal record).
    - For each complete turn, strips non-model-facing record types and
      removes orphan tool_result records (no matching tool_call).
    - Drops oldest complete turns until the total fits within budget.

    Args:
        records: All session records (any order; sorted internally by sequence).
        budget: Maximum number of records to return. The session-scoped
                system_message counts against this budget. When exceeded,
                the oldest complete turns are dropped first.

    Returns:
        Ordered list of records ready for model context injection.
        Deterministic for the same input and budget.
    """
    if not records:
        return []

    ordered = sorted(records, key=lambda r: r.sequence)
    latest_system_msg = _find_latest_session_system_message(ordered)
    complete_turns = _collect_complete_turns(ordered)
    turn_slots = [_filter_turn_records(tr) for tr in complete_turns]
    turn_slots = [s for s in turn_slots if s]

    result: list[SessionRecord] = []
    remaining_budget = budget

    if latest_system_msg and remaining_budget > 0:
        result.append(latest_system_msg)
        remaining_budget -= 1

    while sum(len(s) for s in turn_slots) > remaining_budget and turn_slots:
        turn_slots.pop(0)

    for slot in turn_slots:
        result.extend(slot)
    return result


def _find_latest_session_system_message(
    ordered: list[SessionRecord],
) -> SessionRecord | None:
    latest = None
    for record in ordered:
        if (
            record.record_type == SessionRecordType.SYSTEM_MESSAGE
            and record.payload.get("scope") == SystemMessageScope.SESSION.value
        ):
            latest = record
    return latest


def _collect_complete_turns(ordered: list[SessionRecord]) -> list[list[SessionRecord]]:
    turns: dict[str, list[SessionRecord]] = {}
    turn_order: list[str] = []
    for record in ordered:
        if record.turn_id not in turns:
            turn_order.append(record.turn_id)
            turns[record.turn_id] = []
        turns[record.turn_id].append(record)

    complete: list[list[SessionRecord]] = []
    for turn_id in turn_order:
        turn_records = turns[turn_id]
        if any(r.record_type == SessionRecordType.TURN_TERMINAL for r in turn_records):
            complete.append(turn_records)
    return complete


def _filter_turn_records(turn_records: list[SessionRecord]) -> list[SessionRecord]:
    model_records: list[SessionRecord] = []
    for r in turn_records:
        if r.record_type not in _MODEL_TURN_TYPES:
            continue
        if (
            r.record_type == SessionRecordType.SYSTEM_MESSAGE
            and r.payload.get("scope") == SystemMessageScope.SESSION.value
        ):
            continue
        model_records.append(r)

    # Only include tool call/result pairs where BOTH sides are present.
    # An assistant_tool_call without a result is an open call and must be excluded.
    # A tool_result without a matching call is an orphan and must be excluded.
    call_ids = {
        r.payload.get("tool_call_id")
        for r in model_records
        if r.record_type == SessionRecordType.ASSISTANT_TOOL_CALL
        if r.payload.get("tool_call_id")
    }
    result_ids = {
        r.payload.get("tool_call_id")
        for r in model_records
        if r.record_type == SessionRecordType.TOOL_RESULT
        if r.payload.get("tool_call_id")
    }
    matched_ids = call_ids & result_ids

    return [
        r
        for r in model_records
        if r.record_type
        not in (SessionRecordType.ASSISTANT_TOOL_CALL, SessionRecordType.TOOL_RESULT)
        or r.payload.get("tool_call_id") in matched_ids
    ]
