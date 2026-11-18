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
_DETAIL_MAX_CHARS = 150


def render_confirmation_prompt(request: ConfirmationRequest) -> str:
    """Message text shown above the Approve/Reject buttons."""
    title = _TITLES.get(request.kind, _DEFAULT_TITLE)
    return f"{title}\n\n{request.summary_for_user}"


def status_text(request: ConfirmationRequest | None) -> str:
    """Short status line used for the callback toast and the edited message."""
    if request is None:
        return NO_LONGER_VALID_TEXT
    if request.status == ConfirmationStatus.APPROVED:
        return "✅ Saved."
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
    return f"{base}\n\n{status}" if base.strip() else status
