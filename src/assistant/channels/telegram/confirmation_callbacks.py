"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Signed callback payloads for generic confirmation buttons (Approve/Reject).

Format: ``cf:<confirmation_id>:<a|r>:<sig>``; the HMAC binds chat_id, id and
decision. Expiry is owned by the core confirmation service, not the payload.
Legacy ``mc:`` payloads (pre-generic memory confirmation) are recognised only so
they can be answered as expired instead of reaching the LLM.
"""

import hashlib
import hmac
from dataclasses import dataclass
from enum import StrEnum

from assistant.core.confirmation import ConfirmationDecision

CONFIRMATION_CALLBACK_PREFIX = "cf"
LEGACY_MEMORY_CALLBACK_PREFIX = "mc"
_SIG_LENGTH = 16
_DECISION_CODES = {ConfirmationDecision.APPROVE: "a", ConfirmationDecision.REJECT: "r"}
_CODE_DECISIONS = {code: decision for decision, code in _DECISION_CODES.items()}


class ConfirmationCallbackState(StrEnum):
    """Classification of a confirmation-like callback payload."""

    VALID = "valid"
    INVALID = "invalid"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class ConfirmationCallback:
    """Parsed confirmation callback; id/decision are set only when VALID."""

    state: ConfirmationCallbackState
    confirmation_id: str = ""
    decision: ConfirmationDecision | None = None


def is_confirmation_callback_data(callback_data: str) -> bool:
    """True for any confirmation-shaped payload (current or legacy), valid or not."""
    prefix = callback_data.split(":", 1)[0]
    return ":" in callback_data and prefix in (
        CONFIRMATION_CALLBACK_PREFIX,
        LEGACY_MEMORY_CALLBACK_PREFIX,
    )


def sign_confirmation_callback(
    confirmation_id: str, decision: ConfirmationDecision, chat_id: int, secret: bytes
) -> str:
    """Build the signed callback payload for one confirmation button."""
    code = _DECISION_CODES[decision]
    sig = _signature(chat_id, confirmation_id, code, secret)
    return f"{CONFIRMATION_CALLBACK_PREFIX}:{confirmation_id}:{code}:{sig}"


def parse_confirmation_callback(
    callback_data: str, expected_chat_id: int, secret: bytes
) -> ConfirmationCallback:
    """Classify and verify a confirmation callback payload."""
    parts = callback_data.split(":")
    if parts[0] == LEGACY_MEMORY_CALLBACK_PREFIX:
        return ConfirmationCallback(ConfirmationCallbackState.LEGACY)
    invalid = ConfirmationCallback(ConfirmationCallbackState.INVALID)
    if len(parts) != 4 or parts[0] != CONFIRMATION_CALLBACK_PREFIX:
        return invalid
    _, confirmation_id, code, sig = parts
    decision = _CODE_DECISIONS.get(code)
    if not confirmation_id or decision is None:
        return invalid
    expected = _signature(expected_chat_id, confirmation_id, code, secret)
    if not hmac.compare_digest(sig, expected):
        return invalid
    return ConfirmationCallback(ConfirmationCallbackState.VALID, confirmation_id, decision)


def _signature(chat_id: int, confirmation_id: str, code: str, secret: bytes) -> str:
    msg = f"{chat_id}:{confirmation_id}:{code}"
    return hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:_SIG_LENGTH]
