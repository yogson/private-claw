"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Signed callback helpers for delegation AskUserQuestion option buttons.
"""

import hashlib
import hmac
from datetime import UTC, datetime

_COMPACT_HMAC_SIG_LENGTH = 12
CALLBACK_TTL_SECONDS = 3600
COMPACT_CALLBACK_ACTION = "aq"


def sign_ask_question_callback(token: str, chat_id: int, secret: bytes) -> str:
    """Build compact signed callback payload for delegation question option buttons."""
    ts = int(datetime.now(UTC).timestamp())
    ts36 = format(ts, "x")
    msg = f"{chat_id}:{token}:{ts36}"
    sig = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:_COMPACT_HMAC_SIG_LENGTH]
    return f"{COMPACT_CALLBACK_ACTION}:{token}:{ts36}:{sig}"


def verify_ask_question_callback(
    callback_data: str, expected_chat_id: int, secret: bytes
) -> str | None:
    """Return verified token for compact delegation question callback payloads, or None."""
    now_ts = int(datetime.now(UTC).timestamp())
    without_sig, _, sig = callback_data.rpartition(":")
    if not without_sig or not sig:
        return None
    without_ts, _, ts36 = without_sig.rpartition(":")
    if not without_ts or not ts36:
        return None
    action, _, token = without_ts.partition(":")
    if action != COMPACT_CALLBACK_ACTION or not token:
        return None
    try:
        ts = int(ts36, 16)
    except ValueError:
        return None
    age = now_ts - ts
    if age < 0 or age > CALLBACK_TTL_SECONDS:
        return None
    msg = f"{expected_chat_id}:{token}:{ts36}"
    expected = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:_COMPACT_HMAC_SIG_LENGTH]
    if not hmac.compare_digest(sig, expected):
        return None
    return token
