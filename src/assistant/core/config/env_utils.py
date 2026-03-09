"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Environment variable override utilities for config domain loading.

Naming convention: ASSISTANT_<DOMAIN>_<KEY> for top-level fields,
ASSISTANT_<DOMAIN>_<PARENT>__<CHILD> (double-underscore) for nested fields.
List and dict values must be JSON-encoded strings.
"""

import json
import os
from typing import Any


def parse_env_value(raw: str) -> Any:
    """Parse an env string into a typed value via JSON, falling back to raw string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def deep_set(data: dict[str, Any], key_parts: list[str], value: Any) -> None:
    """Set a value at an arbitrary depth in a dict, creating intermediate dicts."""
    for key in key_parts[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[key_parts[-1]] = value


def apply_env_overrides(data: dict[str, Any], prefix: str) -> tuple[dict[str, Any], set[str]]:
    """Apply ASSISTANT_<DOMAIN>_* env vars onto a config dict.

    Returns the merged dict and the set of top-level field names that were
    overridden by an env var (used for provenance tracking).

    Supports:
    - Top-level: ASSISTANT_APP_LOG_LEVEL=DEBUG → data["log_level"] = "DEBUG"
    - Nested (double-underscore): ASSISTANT_SCHEDULER_RETRY_POLICY__MAX_ATTEMPTS=5
      → data["retry_policy"]["max_attempts"] = 5
    - Typed values: JSON-parse attempted first (handles lists, ints, bools, dicts).
    """
    result = dict(data)
    env_overridden: set[str] = set()
    upper_prefix = prefix.upper() + "_"

    for env_key, env_val in os.environ.items():
        if not env_key.upper().startswith(upper_prefix):
            continue
        field_key = env_key[len(upper_prefix) :].lower()
        parsed = parse_env_value(env_val)
        if "__" in field_key:
            parts = field_key.split("__")
            deep_set(result, parts, parsed)
            env_overridden.add(parts[0])
        else:
            result[field_key] = parsed
            env_overridden.add(field_key)

    return result, env_overridden
