"""
Shared encoding utilities for language-learning exercise tools.
"""

import base64
import gzip
import json
from typing import Any

from assistant.extensions.language_learning.models import CompactWordPayload


def encode_words(words: list[Any]) -> str:
    """Encode words as CompactWordPayload list → JSON → gzip → base64url."""
    payloads = [
        CompactWordPayload.from_entry(w).model_dump(by_alias=True, exclude_none=True) for w in words
    ]
    json_bytes = json.dumps(payloads, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(json_bytes, compresslevel=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
