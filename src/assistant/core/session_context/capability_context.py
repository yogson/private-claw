"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Per-chat capability override storage for Telegram sessions.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)

_STORAGE_VERSION = 2
_DEFAULT_MAX_AGE_DAYS = 30


class SessionCapabilityContextInterface(Protocol):
    """Interface for per-chat capability override storage."""

    def get_capabilities(self, context_id: str) -> list[str] | None:
        """Return capability list override for the given context, or None if not set."""

    def set_capabilities(self, context_id: str, capabilities: list[str]) -> None:
        """Set capability list override for the given context."""

    def clear_capabilities(self, context_id: str) -> None:
        """Remove capability override for the given context."""


class SessionCapabilityContextService:
    """Persisted per-chat capability override storage.

    Mirrors the ``SessionModelContextService`` pattern but stores ``list[str]``
    values instead of a single model-id string.  An empty list ``[]`` is a valid
    override (all capabilities disabled) and is stored/loaded faithfully.

    Stale entries are pruned automatically: any context whose capabilities were
    last set more than ``max_age_days`` days ago is evicted on the next save.
    This prevents the JSON store from growing without bound in long-running
    deployments (one entry per session, never previously evicted).
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
        if not self._capability_overrides:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self._max_age_days)
        stale: list[str] = []
        for ctx_id in list(self._capability_overrides):
            ts_str = self._timestamps.get(ctx_id, "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts < cutoff:
                    stale.append(ctx_id)
            except (ValueError, TypeError):
                # Unparseable or missing timestamp — treat the entry as stale.
                stale.append(ctx_id)
        for ctx_id in stale:
            self._capability_overrides.pop(ctx_id, None)
            self._timestamps.pop(ctx_id, None)
        if stale:
            logger.info(
                "capability_context.pruned_stale_entries",
                count=len(stale),
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
            }
            encoded = json.dumps(payload)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(encoded)
            tmp_path.replace(path)
        except OSError:
            logger.warning("capability_context.save_failed", path=str(path))
