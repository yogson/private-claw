"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Per-session capability state for Telegram sessions: the ``/capabilities`` override and the
set of capabilities added with ``capability_load``.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)

# Version 2 plus the optional "loaded" / "loaded_timestamps" maps; older readers of version 2
# ignore the extra keys, so the format stays readable after a downgrade.
_STORAGE_VERSION = 2
_DEFAULT_MAX_AGE_DAYS = 30
# A session's loaded set is kept while the session is used: every turn refreshes its
# timestamp, but writes the file at most this often.
_LOADED_TOUCH_INTERVAL = timedelta(days=1)


class SessionCapabilityContextInterface(Protocol):
    """Interface for per-chat capability override storage."""

    def get_capabilities(self, context_id: str) -> list[str] | None:
        """Return capability list override for the given context, or None if not set."""

    def set_capabilities(self, context_id: str, capabilities: list[str]) -> None:
        """Set capability list override for the given context."""

    def clear_capabilities(self, context_id: str) -> None:
        """Remove capability override for the given context."""

    def get_loaded_capabilities(self, session_id: str) -> list[str]:
        """Return the capabilities loaded with capability_load, in load order."""

    def set_loaded_capabilities(self, session_id: str, capabilities: list[str]) -> None:
        """Replace the loaded set of ``session_id`` (an empty list clears it)."""

    def touch_loaded_capabilities(self, session_id: str) -> None:
        """Mark the loaded set of ``session_id`` as in use (keeps it from being pruned)."""


class SessionCapabilityContextService:
    """Persisted per-chat capability override storage.

    Mirrors the ``SessionModelContextService`` pattern but stores ``list[str]``
    values instead of a single model-id string.  An empty list ``[]`` is a valid
    override (all capabilities disabled) and is stored/loaded faithfully.

    Stale entries are pruned automatically: any context whose capabilities were
    last set more than ``max_age_days`` days ago is evicted on the next save.
    This prevents the JSON store from growing without bound in long-running
    deployments (one entry per session, never previously evicted).

    The loaded set (``capability_load``) is a separate map keyed by session id with its own
    timestamps. It is independent of the override (changing the override keeps it) and is
    pruned after ``max_age_days`` without a turn: :meth:`touch_loaded_capabilities` runs on
    every turn, so a session that is still in use keeps its loaded set. A new session id
    starts empty. It is not derived from message history, so compaction cannot lose it.
    """

    def __init__(
        self,
        storage_path: Path | None = None,
        max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self._storage_path = storage_path
        self._max_age_days = max_age_days
        self._capability_overrides: dict[str, list[str]] = {}
        self._timestamps: dict[str, str] = {}
        self._loaded: dict[str, list[str]] = {}
        self._loaded_timestamps: dict[str, str] = {}
        self._load()

    def get_capabilities(self, context_id: str) -> list[str] | None:
        """Return capability list for *context_id*, or ``None`` if no override is set."""
        normalized = context_id.strip()
        if not normalized:
            return None
        return self._capability_overrides.get(normalized)

    def set_capabilities(self, context_id: str, capabilities: list[str]) -> None:
        """Persist *capabilities* as the override for *context_id*."""
        normalized = context_id.strip()
        if not normalized:
            return
        self._capability_overrides[normalized] = list(capabilities)
        self._timestamps[normalized] = datetime.now(UTC).isoformat()
        self._save()

    def clear_capabilities(self, context_id: str) -> None:
        """Remove capability override for *context_id*; no-op if not set."""
        normalized = context_id.strip()
        if not normalized:
            return
        self._capability_overrides.pop(normalized, None)
        self._timestamps.pop(normalized, None)
        self._save()

    def get_loaded_capabilities(self, session_id: str) -> list[str]:
        """Return a copy of the loaded set of *session_id* (empty when none)."""
        return list(self._loaded.get(session_id.strip(), ()))

    def set_loaded_capabilities(self, session_id: str, capabilities: list[str]) -> None:
        """Persist *capabilities* (deduplicated, order kept) as the loaded set."""
        normalized = session_id.strip()
        if not normalized:
            return
        clean = list(dict.fromkeys(c.strip() for c in capabilities if c.strip()))
        if clean:
            self._loaded[normalized] = clean
            self._loaded_timestamps[normalized] = datetime.now(UTC).isoformat()
        elif normalized in self._loaded:
            self._loaded.pop(normalized, None)
            self._loaded_timestamps.pop(normalized, None)
        else:
            return
        self._save()

    def touch_loaded_capabilities(self, session_id: str) -> None:
        """Refresh the loaded set's timestamp; writes only if it is older than a day."""
        normalized = session_id.strip()
        if normalized not in self._loaded:
            return
        now = datetime.now(UTC)
        last = _parse_timestamp(self._loaded_timestamps.get(normalized, ""))
        if last is not None and now - last < _LOADED_TOUCH_INTERVAL:
            return
        self._loaded_timestamps[normalized] = now.isoformat()
        self._save()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        path = self._storage_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("capability_context.load_failed", path=str(path))
            return
        if not isinstance(raw, dict):
            return

        # Detect format version.  Old format is a flat dict {context_id: [...]}.
        if raw.get("__version") == _STORAGE_VERSION:
            contexts = raw.get("contexts", {})
            timestamps = raw.get("timestamps", {})
            self._loaded, self._loaded_timestamps = _parse_string_lists(
                raw.get("loaded", {}), raw.get("loaded_timestamps", {})
            )
        else:
            # Migrate from old flat-dict format.  Assign the current time to all
            # entries so nothing is immediately pruned on the first upgraded load.
            contexts = raw
            timestamps = {}
            now_iso = datetime.now(UTC).isoformat()
            for ctx_id in contexts:
                timestamps[ctx_id] = now_iso

        if not isinstance(contexts, dict):
            return

        loaded: dict[str, list[str]] = {}
        loaded_ts: dict[str, str] = {}
        for ctx_id, caps in contexts.items():
            if not isinstance(ctx_id, str):
                continue
            ctx_clean = ctx_id.strip()
            if not ctx_clean:
                continue
            # Accept an empty list as a valid "all disabled" override.
            if not isinstance(caps, list):
                continue
            clean_caps = [c for c in caps if isinstance(c, str) and c.strip()]
            loaded[ctx_clean] = clean_caps
            ts = timestamps.get(ctx_id, "")
            if isinstance(ts, str) and ts:
                loaded_ts[ctx_clean] = ts

        self._capability_overrides = loaded
        self._timestamps = loaded_ts
        self._prune_stale_entries()

    def _prune_stale_entries(self) -> None:
        """Remove entries whose last-set timestamp is older than ``max_age_days`` days."""
        cutoff = datetime.now(UTC) - timedelta(days=self._max_age_days)
        stale = _stale_keys(self._capability_overrides, self._timestamps, cutoff)
        for ctx_id in stale:
            self._capability_overrides.pop(ctx_id, None)
            self._timestamps.pop(ctx_id, None)
        stale_loaded = _stale_keys(self._loaded, self._loaded_timestamps, cutoff)
        for session_id in stale_loaded:
            self._loaded.pop(session_id, None)
            self._loaded_timestamps.pop(session_id, None)
        if stale or stale_loaded:
            logger.info(
                "capability_context.pruned_stale_entries",
                count=len(stale),
                loaded_count=len(stale_loaded),
                max_age_days=self._max_age_days,
            )

    def _save(self) -> None:
        path = self._storage_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "__version": _STORAGE_VERSION,
                "contexts": self._capability_overrides,
                "timestamps": self._timestamps,
                "loaded": self._loaded,
                "loaded_timestamps": self._loaded_timestamps,
            }
            encoded = json.dumps(payload)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(encoded)
            tmp_path.replace(path)
        except OSError:
            logger.warning("capability_context.save_failed", path=str(path))


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _stale_keys(
    entries: dict[str, list[str]], timestamps: dict[str, str], cutoff: datetime
) -> list[str]:
    """Keys whose timestamp is older than ``cutoff``; a missing/unparseable one is stale."""
    stale: list[str] = []
    for key in entries:
        ts = _parse_timestamp(timestamps.get(key, ""))
        if ts is None or ts < cutoff:
            stale.append(key)
    return stale


def _parse_string_lists(
    raw_entries: object, raw_timestamps: object
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Validate a persisted ``{key: [str, ...]}`` map and its ``{key: iso-ts}`` timestamps."""
    if not isinstance(raw_entries, dict):
        return {}, {}
    timestamps = raw_timestamps if isinstance(raw_timestamps, dict) else {}
    entries: dict[str, list[str]] = {}
    entry_ts: dict[str, str] = {}
    for key, values in raw_entries.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(values, list):
            continue
        clean = list(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))
        if not clean:
            continue
        entries[key.strip()] = clean
        ts = timestamps.get(key, "")
        if isinstance(ts, str) and ts:
            entry_ts[key.strip()] = ts
    return entries, entry_ts
