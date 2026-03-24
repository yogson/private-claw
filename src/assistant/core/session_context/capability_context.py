"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Per-chat capability override storage for Telegram sessions.
"""

import json
from pathlib import Path
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


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
    """

    def __init__(self, storage_path: Path | None = None) -> None:
        self._storage_path = storage_path
        self._capability_overrides: dict[str, list[str]] = self._load()

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
        self._save()

    def clear_capabilities(self, context_id: str) -> None:
        """Remove capability override for *context_id*; no-op if not set."""
        normalized = context_id.strip()
        if not normalized:
            return
        self._capability_overrides.pop(normalized, None)
        self._save()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, list[str]]:
        path = self._storage_path
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("capability_context.load_failed", path=str(path))
            return {}
        if not isinstance(raw, dict):
            return {}
        loaded: dict[str, list[str]] = {}
        for ctx_id, caps in raw.items():
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
        return loaded

    def _save(self) -> None:
        path = self._storage_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(self._capability_overrides)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(encoded)
            tmp_path.replace(path)
        except OSError:
            logger.warning("capability_context.save_failed", path=str(path))
