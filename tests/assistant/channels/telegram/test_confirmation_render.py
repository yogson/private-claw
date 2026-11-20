"""Tests for Telegram confirmation texts: per-kind labels and the 4096 UTF-16 limit."""

from datetime import UTC, datetime, timedelta

from assistant.channels.telegram.confirmation_render import (
    NO_LONGER_VALID_TEXT,
    NOT_DELIVERED_TEXT,
    confirmation_tag,
    render_confirmation_prompt,
    render_resolved_text,
    status_text,
)
from assistant.channels.telegram.formatter import format_markdown_for_telegram
from assistant.core.confirmation import (
    NOT_DELIVERED_OUTCOME_STATUS,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationStatus,
)

_LIMIT = 4096


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _request(
    summary: str,
    kind: str = "memory_update",
    status: ConfirmationStatus = ConfirmationStatus.PENDING,
    detail: str = "",
) -> ConfirmationRequest:
    now = datetime.now(UTC)
    return ConfirmationRequest(
        confirmation_id="cid",
        kind=kind,
        session_id="tg:1",
        summary_for_user=summary,
        dedupe_key="k",
        status=status,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        result=ConfirmationOutcome(ok=False, status="x", detail=detail) if detail else None,
    )


def test_approved_label_is_per_kind() -> None:
    assert status_text(_request("s", status=ConfirmationStatus.APPROVED)) == "✅ Saved."
    edit = _request("s", kind="capability_edit", status=ConfirmationStatus.APPROVED)
    assert status_text(edit) == "✅ Applied."
    other = _request("s", kind="some_future_kind", status=ConfirmationStatus.APPROVED)
    assert status_text(other) == "✅ Approved."


def test_oversized_prompt_is_one_message_and_marks_truncation() -> None:
    # Astral-plane emoji count as 2 UTF-16 units each in Telegram's limit.
    request = _request("🧠" * 5000)

    prompt = render_confirmation_prompt(request)

    assert _utf16(prompt) <= _LIMIT - 200
    assert prompt.endswith("[truncated]")
    assert len(format_markdown_for_telegram(prompt)) == 1


def test_resolved_edit_of_oversized_prompt_keeps_the_status_line() -> None:
    detail = "d" * 1000
    request = _request("x" * 9000, status=ConfirmationStatus.FAILED, detail=detail)

    edited = render_resolved_text("", request)

    assert _utf16(edited) <= _LIMIT
    assert edited.endswith(status_text(request))
    assert status_text(request).startswith("⚠️ Failed: ")


def test_resolved_edit_of_unknown_request_truncates_original_text() -> None:
    edited = render_resolved_text("o" * _LIMIT, None)

    assert _utf16(edited) <= _LIMIT
    assert edited.endswith(NO_LONGER_VALID_TEXT)


def test_short_prompt_is_unchanged() -> None:
    request = _request("Action: upsert\n\nUser prefers tea.")
    assert render_confirmation_prompt(request) == (
        "🧠 Save this to memory?\n\nAction: upsert\n\nUser prefers tea."
    )


def test_failed_not_delivered_status_is_neutral_other_failures_show_detail() -> None:
    undelivered = _request("s", status=ConfirmationStatus.FAILED)
    undelivered.result = ConfirmationOutcome(
        ok=False,
        status=NOT_DELIVERED_OUTCOME_STATUS,
        detail="The confirmation buttons could not be delivered to the user.",
    )
    assert status_text(undelivered) == NOT_DELIVERED_TEXT

    apply_failed = _request("s", status=ConfirmationStatus.FAILED, detail="Apply failed: disk")
    assert status_text(apply_failed) == "⚠️ Failed: Apply failed: disk"


def test_confirmation_tag_is_keyed_not_a_prefix_of_the_id() -> None:
    # S12 of #64: the agent knows the confirmation id, so the tag on the parts and the
    # button message must not be computable from it.
    import re
    import uuid

    ids = [uuid.uuid4().hex for _ in range(50)]
    tags = [confirmation_tag(_request("s").model_copy(update={"confirmation_id": i})) for i in ids]
    assert all(re.fullmatch(r"[0-9a-f]{8}", t) for t in tags)
    assert sum(t == i[:8] for t, i in zip(tags, ids, strict=True)) == 0
    assert len(set(tags)) == len(tags)
    request = _request("s")
    assert confirmation_tag(request) == confirmation_tag(request)  # stable for re-renders
