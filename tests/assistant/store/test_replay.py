"""Tests for session replay logic (build_replay and FilesystemSessionStore.replay_for_turn)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.store.filesystem.replay import build_replay
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.models import (
    SessionRecord,
    SessionRecordType,
    SystemMessageScope,
    TurnTerminalStatus,
)


def _rec(
    session_id: str = "s1",
    sequence: int = 0,
    turn_id: str = "t1",
    event_id: str | None = None,
    record_type: SessionRecordType = SessionRecordType.USER_MESSAGE,
    payload: dict | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        sequence=sequence,
        event_id=event_id or f"evt-{sequence}",
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        record_type=record_type,
        payload=payload or {"message_id": f"m-{sequence}", "content": "test"},
    )


def _terminal(
    session_id: str = "s1",
    sequence: int = 99,
    turn_id: str = "t1",
    status: TurnTerminalStatus = TurnTerminalStatus.COMPLETED,
) -> SessionRecord:
    return _rec(
        session_id=session_id,
        sequence=sequence,
        turn_id=turn_id,
        record_type=SessionRecordType.TURN_TERMINAL,
        payload={"status": status.value},
    )


def _system(
    sequence: int,
    scope: SystemMessageScope = SystemMessageScope.SESSION,
    turn_id: str = "t0",
) -> SessionRecord:
    return _rec(
        sequence=sequence,
        turn_id=turn_id,
        record_type=SessionRecordType.SYSTEM_MESSAGE,
        payload={
            "message_id": f"sys-{sequence}",
            "content": "system prompt",
            "scope": scope.value,
        },
    )


# ---------------------------------------------------------------------------
# build_replay – unit tests (pure function)
# ---------------------------------------------------------------------------


def test_empty_records_returns_empty() -> None:
    assert build_replay([], budget=10) == []


def test_incomplete_turn_excluded() -> None:
    """Turns without turn_terminal are not included in replay."""
    records = [
        _rec(sequence=0, turn_id="t1"),
        _rec(sequence=1, turn_id="t1", record_type=SessionRecordType.ASSISTANT_MESSAGE),
    ]
    result = build_replay(records, budget=20)
    assert result == []


def test_complete_turn_included() -> None:
    records = [
        _rec(sequence=0, turn_id="t1"),
        _rec(sequence=1, turn_id="t1", record_type=SessionRecordType.ASSISTANT_MESSAGE),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    assert len(result) == 2
    assert all(r.turn_id == "t1" for r in result)
    types = {r.record_type for r in result}
    assert SessionRecordType.TURN_TERMINAL not in types


def test_turn_summary_excluded() -> None:
    records = [
        _rec(sequence=0, turn_id="t1"),
        _rec(
            sequence=1,
            turn_id="t1",
            record_type=SessionRecordType.TURN_SUMMARY,
            payload={"summary_text": "summary"},
        ),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    assert all(r.record_type != SessionRecordType.TURN_SUMMARY for r in result)


def test_session_system_message_prepended() -> None:
    records = [
        _system(sequence=0),
        _rec(sequence=1, turn_id="t1"),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    assert result[0].record_type == SessionRecordType.SYSTEM_MESSAGE
    assert result[0].payload["scope"] == SystemMessageScope.SESSION.value


def test_only_latest_session_system_message_included() -> None:
    records = [
        _system(sequence=0, scope=SystemMessageScope.SESSION),
        _system(sequence=1, scope=SystemMessageScope.SESSION),
        _rec(sequence=2, turn_id="t1"),
        _terminal(sequence=3, turn_id="t1"),
    ]
    system_msgs = [
        r
        for r in build_replay(records, budget=20)
        if r.record_type == SessionRecordType.SYSTEM_MESSAGE
    ]
    assert len(system_msgs) == 1
    assert system_msgs[0].sequence == 1


def test_turn_scope_system_message_included_in_turn() -> None:
    records = [
        _system(sequence=0, scope=SystemMessageScope.TURN, turn_id="t1"),
        _rec(sequence=1, turn_id="t1"),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    types = [r.record_type for r in result]
    assert SessionRecordType.SYSTEM_MESSAGE in types


def test_open_tool_call_excluded() -> None:
    """assistant_tool_call without a matching tool_result must not appear in replay."""
    records = [
        _rec(
            sequence=0,
            turn_id="t1",
            record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
            payload={
                "message_id": "m0",
                "tool_call_id": "call-open",
                "tool_name": "my_tool",
                "arguments_json": "{}",
            },
        ),
        _terminal(sequence=1, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    assert all(r.record_type != SessionRecordType.ASSISTANT_TOOL_CALL for r in result)


def test_orphan_tool_result_excluded() -> None:
    records = [
        _rec(sequence=0, turn_id="t1"),
        _rec(
            sequence=1,
            turn_id="t1",
            record_type=SessionRecordType.TOOL_RESULT,
            payload={"message_id": "m1", "tool_call_id": "no-call", "tool_name": "x"},
        ),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    assert all(r.record_type != SessionRecordType.TOOL_RESULT for r in result)


def test_malformed_tool_records_without_tool_call_id_excluded() -> None:
    """Records missing tool_call_id must not form a phantom pair and must be excluded."""
    records = [
        _rec(
            sequence=0,
            turn_id="t1",
            record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
            payload={"message_id": "m0", "tool_name": "my_tool", "arguments_json": "{}"},
        ),
        _rec(
            sequence=1,
            turn_id="t1",
            record_type=SessionRecordType.TOOL_RESULT,
            payload={"message_id": "m1", "tool_name": "my_tool", "result": "ok"},
        ),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    assert all(r.record_type != SessionRecordType.ASSISTANT_TOOL_CALL for r in result)
    assert all(r.record_type != SessionRecordType.TOOL_RESULT for r in result)


def test_matched_tool_call_and_result_both_included() -> None:
    records = [
        _rec(
            sequence=0,
            turn_id="t1",
            record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
            payload={
                "message_id": "m0",
                "tool_call_id": "call-1",
                "tool_name": "my_tool",
                "arguments_json": "{}",
            },
        ),
        _rec(
            sequence=1,
            turn_id="t1",
            record_type=SessionRecordType.TOOL_RESULT,
            payload={
                "message_id": "m1",
                "tool_call_id": "call-1",
                "tool_name": "my_tool",
                "result": "ok",
            },
        ),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    types = [r.record_type for r in result]
    assert SessionRecordType.ASSISTANT_TOOL_CALL in types
    assert SessionRecordType.TOOL_RESULT in types


def test_budget_drops_oldest_turn_first() -> None:
    """When 2 complete turns exist and budget only fits 1, the oldest is dropped."""
    records = [
        _rec(sequence=0, turn_id="t1"),
        _terminal(sequence=1, turn_id="t1"),
        _rec(sequence=2, turn_id="t2"),
        _terminal(sequence=3, turn_id="t2"),
    ]
    result = build_replay(records, budget=1)
    assert all(r.turn_id == "t2" for r in result)


def test_budget_zero_returns_empty() -> None:
    records = [
        _rec(sequence=0, turn_id="t1"),
        _terminal(sequence=1, turn_id="t1"),
    ]
    assert build_replay(records, budget=0) == []


def test_budget_zero_with_session_system_message_returns_empty() -> None:
    """Session system message must not leak into result when budget is 0."""
    records = [
        _system(sequence=0),
        _rec(sequence=1, turn_id="t1"),
        _terminal(sequence=2, turn_id="t1"),
    ]
    assert build_replay(records, budget=0) == []


def test_deterministic_output() -> None:
    """Same input and budget always produce the same output."""
    records = [
        _rec(sequence=0, turn_id="t1"),
        _terminal(sequence=1, turn_id="t1"),
    ]
    r1 = build_replay(records, budget=10)
    r2 = build_replay(records, budget=10)
    assert [r.sequence for r in r1] == [r.sequence for r in r2]


def test_ordering_preserved() -> None:
    """Records within a turn are returned in sequence order."""
    records = [
        _rec(sequence=0, turn_id="t1"),
        _rec(sequence=1, turn_id="t1", record_type=SessionRecordType.ASSISTANT_MESSAGE),
        _terminal(sequence=2, turn_id="t1"),
    ]
    result = build_replay(records, budget=20)
    assert result[0].sequence == 0
    assert result[1].sequence == 1


def test_multiple_complete_turns_ordered() -> None:
    records = [
        _rec(sequence=0, turn_id="t1"),
        _terminal(sequence=1, turn_id="t1"),
        _rec(sequence=2, turn_id="t2"),
        _terminal(sequence=3, turn_id="t2"),
    ]
    result = build_replay(records, budget=20)
    seqs = [r.sequence for r in result]
    assert seqs == sorted(seqs)


def test_incomplete_turn_mixed_with_complete() -> None:
    """Only the complete turn appears; the incomplete one is excluded."""
    records = [
        _rec(sequence=0, turn_id="t1"),
        _terminal(sequence=1, turn_id="t1"),
        _rec(sequence=2, turn_id="t2"),
    ]
    result = build_replay(records, budget=20)
    assert all(r.turn_id == "t1" for r in result)


# ---------------------------------------------------------------------------
# FilesystemSessionStore.replay_for_turn – integration
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


@pytest.fixture
def session_store(sessions_dir: Path) -> FilesystemSessionStore:
    return FilesystemSessionStore(sessions_dir)


@pytest.mark.asyncio
async def test_replay_for_turn_empty_session(session_store: FilesystemSessionStore) -> None:
    result = await session_store.replay_for_turn("no-session", budget=20)
    assert result == []


@pytest.mark.asyncio
async def test_replay_for_turn_complete_turn(session_store: FilesystemSessionStore) -> None:
    await session_store.append(
        [
            _rec(session_id="s1", sequence=0, turn_id="t1", event_id="e0"),
            _terminal(session_id="s1", sequence=1, turn_id="t1"),
        ]
    )
    result = await session_store.replay_for_turn("s1", budget=20)
    assert len(result) == 1
    assert result[0].record_type == SessionRecordType.USER_MESSAGE


@pytest.mark.asyncio
async def test_replay_for_turn_budget_enforced(session_store: FilesystemSessionStore) -> None:
    await session_store.append_raw(
        [
            _rec(session_id="s1", sequence=0, turn_id="t1", event_id="e0"),
            _terminal(session_id="s1", sequence=1, turn_id="t1"),
            _rec(session_id="s1", sequence=2, turn_id="t2", event_id="e2"),
            _terminal(session_id="s1", sequence=3, turn_id="t2"),
        ]
    )
    result = await session_store.replay_for_turn("s1", budget=1)
    assert all(r.turn_id == "t2" for r in result)
