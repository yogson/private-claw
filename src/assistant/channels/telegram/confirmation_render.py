"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram texts for confirmation prompts, callback answers, and resolved-message edits.
"""

from assistant.core.confirmation import ConfirmationRequest, ConfirmationStatus

APPROVE_LABEL = "✅ Approve"
REJECT_LABEL = "❌ Reject"
NO_LONGER_VALID_TEXT = "⌛ Expired: this confirmation is no longer valid."
_TITLES = {"memory_update": "🧠 Save this to memory?"}
_DEFAULT_TITLE = "Confirmation required"
_APPROVED_TEXTS = {"memory_update": "✅ Saved."}
_DEFAULT_APPROVED_TEXT = "✅ Approved."
_DETAIL_MAX_CHARS = 150
# Telegram limits a message to 4096 UTF-16 code units. The prompt keeps room for
# the status line appended by the final edit, so both always fit one message.
_TELEGRAM_MAX_UTF16 = 4096
_STATUS_RESERVE_UTF16 = 300
_TRUNCATED_NOTE = "\n… [truncated]"


def render_confirmation_prompt(request: ConfirmationRequest) -> str:
    """Message text shown above the Approve/Reject buttons."""
    title = _TITLES.get(request.kind, _DEFAULT_TITLE)
    text = f"{title}\n\n{request.summary_for_user}"
    return _fit_utf16(text, _TELEGRAM_MAX_UTF16 - _STATUS_RESERVE_UTF16)


def status_text(request: ConfirmationRequest | None) -> str:
    """Short status line used for the callback toast and the edited message."""
    if request is None:
        return NO_LONGER_VALID_TEXT
    if request.status == ConfirmationStatus.APPROVED:
        return _APPROVED_TEXTS.get(request.kind, _DEFAULT_APPROVED_TEXT)
    if request.status == ConfirmationStatus.REJECTED:
        return "❌ Rejected."
    if request.status == ConfirmationStatus.EXPIRED:
        return NO_LONGER_VALID_TEXT
    if request.status == ConfirmationStatus.FAILED:
        detail = request.detail[:_DETAIL_MAX_CHARS]
        return f"⚠️ Failed: {detail}" if detail else "⚠️ Failed."
    return "⏳ Still pending."


def render_resolved_text(original_text: str, request: ConfirmationRequest | None) -> str:
    """Edited message text: the original content followed by the final status."""
    base = render_confirmation_prompt(request) if request is not None else original_text
    status = status_text(request)
    if not base.strip():
        return status
    room = _TELEGRAM_MAX_UTF16 - _utf16_len(status) - 2
    return f"{_fit_utf16(base, room)}\n\n{status}"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fit_utf16(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` UTF-16 units, marking the cut explicitly."""
    if _utf16_len(text) <= limit:
        return text
    budget = limit - _utf16_len(_TRUNCATED_NOTE)
    used = cut = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        cut += 1
    return text[:cut] + _TRUNCATED_NOTE
