"""Tests for the memory_update confirmation kind (draft building and WriteAudit mapping)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from assistant.core.confirmation import (
    MEMORY_BODY_MAX_CHARS,
    ConfirmationRequest,
    MemoryUpdateHandler,
    build_memory_update_draft,
    memory_body_length_error,
)
from assistant.core.confirmation.service import SUMMARY_MAX_CHARS
from assistant.memory.write.models import MemoryUpdateIntent, WriteAudit, WriteStatus


def _intent(intent_id: str = "i1", body: str = "User prefers tea.") -> MemoryUpdateIntent:
    return MemoryUpdateIntent.model_validate(
        {
            "intent_id": intent_id,
            "action": "upsert",
            "memory_type": "preferences",
            "candidate": {"body_markdown": body, "confidence": 0.9},
            "reason": "explicit ask",
        }
    )


def _request(intent: MemoryUpdateIntent) -> ConfirmationRequest:
    draft = build_memory_update_draft(intent, origin_tool_call_id="tc-1")
    now = datetime.now(UTC)
    return ConfirmationRequest(
        confirmation_id="cid1",
        kind=draft.kind,
        session_id="tg:1",
        payload=draft.payload,
        summary_for_user=draft.summary_for_user,
        dedupe_key=draft.dedupe_key,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


def test_draft_summary_shows_actual_content() -> None:
    draft = build_memory_update_draft(_intent())
    assert draft.kind == "memory_update"
    assert "Action: upsert" in draft.summary_for_user
    assert "Type: preferences" in draft.summary_for_user
    assert "User prefers tea." in draft.summary_for_user


def test_delete_summary_shows_target_memory_id() -> None:
    intent = MemoryUpdateIntent.model_validate(
        {"intent_id": "i", "action": "delete", "memory_type": "facts", "memory_id": "mem-9"}
    )
    assert "Memory ID: mem-9" in build_memory_update_draft(intent).summary_for_user


def test_dedupe_key_ignores_model_intent_id_and_whitespace() -> None:
    a = build_memory_update_draft(_intent("a", "User  prefers tea."))
    b = build_memory_update_draft(_intent("b", "User prefers tea."))
    c = build_memory_update_draft(_intent("c", "User prefers coffee."))
    assert a.dedupe_key == b.dedupe_key != c.dedupe_key


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        (WriteStatus.WRITTEN, True),
        (WriteStatus.UPDATED, True),
        (WriteStatus.DELETED, True),
        (WriteStatus.TOUCHED, True),
        (WriteStatus.REJECTED_INVALID, False),
        (WriteStatus.SKIPPED_LOW_CONFIDENCE, False),
        (WriteStatus.SKIPPED_DEDUP_MERGED, False),
        (WriteStatus.IDEMPOTENT_NOOP, False),
    ],
)
async def test_write_status_mapping(status: WriteStatus, ok: bool) -> None:
    writer = MagicMock()
    writer.apply_intent.return_value = WriteAudit(intent_id="x", status=status, reason="why")
    outcome = await MemoryUpdateHandler(writer).apply(_request(_intent()), user_id="u1")
    assert outcome.ok is ok
    assert outcome.status == status.value


async def test_apply_uses_confirmation_scoped_intent_id_and_user() -> None:
    writer = MagicMock()
    writer.apply_intent.return_value = WriteAudit(
        intent_id="x", status=WriteStatus.WRITTEN, memory_id="mem-1"
    )
    outcome = await MemoryUpdateHandler(writer).apply(_request(_intent()), user_id=None)

    intent, user_id = writer.apply_intent.call_args.args
    assert intent.intent_id == "confirmation:cid1"
    assert user_id == "tg:1"  # falls back to the session id
    assert "mem-1" in outcome.detail


def test_reason_and_memory_id_are_capped_in_summary() -> None:
    intent = MemoryUpdateIntent.model_validate(
        {
            "intent_id": "i",
            "action": "upsert",
            "memory_type": "facts",
            "memory_id": "m" * 1000,
            "candidate": {"body_markdown": "b" * MEMORY_BODY_MAX_CHARS},
            "reason": "r" * 5000,
        }
    )
    summary = build_memory_update_draft(intent).summary_for_user
    assert "b" * MEMORY_BODY_MAX_CHARS in summary
    assert "r" * 301 not in summary and "m" * 201 not in summary
    assert len(summary) <= SUMMARY_MAX_CHARS


def test_overlong_body_is_rejected_by_length_check() -> None:
    assert memory_body_length_error(_intent(body="x" * MEMORY_BODY_MAX_CHARS)) is None
    error = memory_body_length_error(_intent(body="x" * (MEMORY_BODY_MAX_CHARS + 1)))
    assert error is not None and str(MEMORY_BODY_MAX_CHARS + 1) in error
