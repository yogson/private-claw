"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Telegram texts for confirmation prompts, attachment parts, callback answers, and
resolved-message edits.

Attachments (e.g. the full prompt diff of a ``capability_edit``) are produced by core,
already split into parts; this module only adds a header to each part. The parts are
sent before the button message, which replies to part 1 (confirmation_handler).
"""

import hashlib
import hmac
import secrets

from assistant.core.confirmation import (
    NOT_DELIVERED_OUTCOME_STATUS,
    ConfirmationRequest,
    ConfirmationStatus,
)

APPROVE_LABEL = "✅ Approve"
REJECT_LABEL = "❌ Reject"
NO_LONGER_VALID_TEXT = "⌛ Expired: this confirmation is no longer valid."
# For a request discarded as undelivered. The send may have failed after the message
# actually arrived (e.g. a timeout), so the user may be looking at these buttons: do
# not claim they could not be delivered.
NOT_DELIVERED_TEXT = "⚠️ This confirmation is no longer valid."
NOT_OWNER_TEXT = "⛔ Only the user who asked can answer this confirmation."
_TITLES = {
    "memory_update": "🧠 Save this to memory?",
    "capability_edit": "🛠 Apply this capability change?",
}
_DEFAULT_TITLE = "Confirmation required"
_APPROVED_TEXTS = {"memory_update": "✅ Saved.", "capability_edit": "✅ Applied."}
_DEFAULT_APPROVED_TEXT = "✅ Approved."
_DETAIL_MAX_CHARS = 150
# Telegram limits a message to 4096 UTF-16 code units. The prompt keeps room for
# the status line appended by the final edit, so both always fit one message.
_TELEGRAM_MAX_UTF16 = 4096
_STATUS_RESERVE_UTF16 = 300
_TRUNCATED_NOTE = "\n… [truncated]"
# Key of the tag on attachment parts and the button message. The agent knows the
# confirmation id (tool result), never this key, so its own text cannot reproduce the
# tag of a real part. Per process: confirmations are not persisted across restarts.
_TAG_KEY = secrets.token_bytes(32)


def confirmation_tag(request: ConfirmationRequest) -> str:
    """The tag shown on attachment parts and the button message: 8 hex chars of an
    HMAC of the confirmation id under a per-process key."""
    digest = hmac.new(_TAG_KEY, request.confirmation_id.encode(), hashlib.sha256)
    return digest.hexdigest()[:8]


def render_confirmation_prompt(request: ConfirmationRequest) -> str:
    """Message text shown above the Approve/Reject buttons."""
    title = _TITLES.get(request.kind, _DEFAULT_TITLE)
    text = f"{title}\n\n{request.summary_for_user}"
    if request.attachments:
        text += "\n\n" + _attachments_note(request)
    return _fit_utf16(text, _TELEGRAM_MAX_UTF16 - _STATUS_RESERVE_UTF16)


def _attachments_note(request: ConfirmationRequest) -> str:
    sent = "; ".join(
        f"{a.title} ({len(a.parts)} part{'' if len(a.parts) == 1 else 's'})"
        for a in request.attachments
    )
    return (
        f"📎 Sent in full above: {sent}. This message is a reply to part 1 "
        f"(confirmation {confirmation_tag(request)}); read every part before deciding."
    )


def render_attachment_messages(request: ConfirmationRequest) -> list[str]:
    """One plain-text message per attachment part, in delivery order, each headed
    ``<title> — part i/N (confirmation <tag>)``. Never truncated: a message that
    would not fit Telegram's limit is reported by :func:`message_fits`."""
    tag = confirmation_tag(request)
    messages: list[str] = []
    for attachment in request.attachments:
        total = len(attachment.parts)
        for index, part in enumerate(attachment.parts, start=1):
            header = f"{attachment.title} — part {index}/{total} (confirmation {tag})"
            messages.append(f"{header}\n\n{part}")
    return messages


def message_fits(text: str) -> bool:
    """True if ``text`` fits one Telegram message (4096 UTF-16 units)."""
    return _utf16_len(text) <= _TELEGRAM_MAX_UTF16


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
        if request.result is not None and request.result.status == NOT_DELIVERED_OUTCOME_STATUS:
            return NOT_DELIVERED_TEXT
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
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


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
