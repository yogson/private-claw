"""Tests for Telegram confirmation texts: per-kind labels and the 4096 UTF-16 limit."""

from datetime import UTC, datetime, timedelta

from assistant.channels.telegram.confirmation_render import (
    NO_LONGER_VALID_TEXT,
    render_confirmation_prompt,
    render_resolved_text,
    status_text,
)
from assistant.channels.telegram.formatter import format_markdown_for_telegram
from assistant.core.confirmation import (
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
    other = _request("s", kind="capability_edit", status=ConfirmationStatus.APPROVED)
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
