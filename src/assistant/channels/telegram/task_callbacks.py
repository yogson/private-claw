"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Callback signing and verification for delegated task action buttons.
"""

import hashlib
import hmac
from datetime import UTC, datetime

_COMPACT_HMAC_SIG_LENGTH = 12
CALLBACK_TTL_SECONDS = 3600
COMPACT_CALLBACK_ACTION = "tk"


def sign_task_callback(task_id: str, action: str, chat_id: int, secret: bytes) -> str:
    """Build compact signed callback payload for delegated task actions."""
    ts = int(datetime.now(UTC).timestamp())
    ts36 = format(ts, "x")
    msg = f"{chat_id}:{task_id}:{action}:{ts36}"
    sig = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:_COMPACT_HMAC_SIG_LENGTH]
    return f"{COMPACT_CALLBACK_ACTION}:{task_id}:{action}:{ts36}:{sig}"


def verify_task_callback(
    callback_data: str,
    expected_chat_id: int,
    secret: bytes,
) -> tuple[str, str] | None:
    """Return (task_id, action) for valid compact task callback payloads."""
    parts = callback_data.split(":")
    if len(parts) != 5:
        return None
    action_prefix, task_id, action, ts36, sig = parts
    if action_prefix != COMPACT_CALLBACK_ACTION:
        return None
    if not task_id or not action:
        return None
    try:
        ts = int(ts36, 16)
    except ValueError:
        return None
    now_ts = int(datetime.now(UTC).timestamp())
    age = now_ts - ts
    if age < 0 or age > CALLBACK_TTL_SECONDS:
        return None
    msg = f"{expected_chat_id}:{task_id}:{action}:{ts36}"
    expected = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:_COMPACT_HMAC_SIG_LENGTH]
    if not hmac.compare_digest(sig, expected):
        return None
    return task_id, action
