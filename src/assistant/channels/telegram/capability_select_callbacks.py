"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Callback signing and verification for capability selection UI.
"""

import hashlib
import hmac
from datetime import UTC, datetime

from assistant.channels.telegram._callback_constants import _COMPACT_HMAC_SIG_LENGTH
CALLBACK_TTL_SECONDS = 3600
COMPACT_CALLBACK_ACTION = "cs"


def sign_capability_callback(capability_id: str, chat_id: int, secret: bytes) -> str:
    """Build compact signed callback payload for capability selection buttons."""
    ts = int(datetime.now(UTC).timestamp())
    ts36 = format(ts, "x")
    msg = f"{chat_id}:{capability_id}:{ts36}"
    sig = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:_COMPACT_HMAC_SIG_LENGTH]
    return f"{COMPACT_CALLBACK_ACTION}:{capability_id}:{ts36}:{sig}"


def verify_capability_callback(
    callback_data: str, expected_chat_id: int, secret: bytes
) -> str | None:
    """Return verified capability_id for compact callback payloads."""
    now_ts = int(datetime.now(UTC).timestamp())
    without_sig, _, sig = callback_data.rpartition(":")
    if not without_sig or not sig:
        return None
    without_ts, _, ts36 = without_sig.rpartition(":")
    if not without_ts or not ts36:
        return None
    action, _, capability_id = without_ts.partition(":")
    if action != COMPACT_CALLBACK_ACTION or not capability_id:
        return None
    try:
        ts = int(ts36, 16)
    except ValueError:
        return None
    age = now_ts - ts
    if age < 0 or age > CALLBACK_TTL_SECONDS:
        return None
    msg = f"{expected_chat_id}:{capability_id}:{ts36}"
    expected = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:_COMPACT_HMAC_SIG_LENGTH]
    if not hmac.compare_digest(sig, expected):
        return None
    return capability_id
