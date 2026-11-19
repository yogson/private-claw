"""
Component ID: CMP_API_AGENT_NODE_INGRESS

``Idempotency-Key`` validation and request fingerprint, matching agent-node.

The fingerprint covers the validated body minus ``deadline`` (callers compute it
as ``now + timeout``, so a retry carries a later value) and ``github_token``
(excluded from the dump, so a rotated token is the same request).
"""

import hashlib
import json
from typing import Any

MAX_IDEMPOTENCY_KEY_LENGTH = 255
FINGERPRINT_EXCLUDED_FIELDS = frozenset({"deadline"})
_FINGERPRINT_VERSION = "v1"


class InvalidIdempotencyKey(ValueError):
    """The header is malformed. The message never contains the value."""


def validate_idempotency_key(values: list[str]) -> str | None:
    """Return the key from the raw header values, or None when the header is absent."""
    if not values:
        return None
    if len(values) > 1:
        raise InvalidIdempotencyKey("Idempotency-Key header must be sent at most once")
    (value,) = values
    if not 1 <= len(value) <= MAX_IDEMPOTENCY_KEY_LENGTH or not all(
        0x20 <= ord(ch) <= 0x7E for ch in value
    ):
        raise InvalidIdempotencyKey(
            f"Idempotency-Key must be 1 to {MAX_IDEMPOTENCY_KEY_LENGTH} printable ASCII characters"
        )
    if value[0] == " " or value[-1] == " ":
        raise InvalidIdempotencyKey("Idempotency-Key must not start or end with a space")
    return value


def request_fingerprint(request: dict[str, Any]) -> str:
    """Fingerprint of a validated body as produced by ``model_dump(mode="json")``."""
    canonical = {k: v for k, v in request.items() if k not in FINGERPRINT_EXCLUDED_FIELDS}
    canonical["files"] = [
        {
            "path": f["path"],
            "content_sha256": hashlib.sha256(f["content_base64"].encode("utf-8")).hexdigest(),
        }
        for f in canonical.get("files") or []
    ]
    document = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{_FINGERPRINT_VERSION}:{hashlib.sha256(document.encode('ascii')).hexdigest()}"
