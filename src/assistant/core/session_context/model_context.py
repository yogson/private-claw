"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Per-chat model override storage for Telegram sessions.
"""

import json
from pathlib import Path
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


class SessionModelContextInterface(Protocol):
    """Interface for per-chat model override storage."""

    def get_model_override(self, context_id: str) -> str | None:
        """Return model_id override for the given context, or None."""

    def set_model_override(self, context_id: str, model_id: str) -> None:
        """Set model_id override for the given context."""

    def clear_model_override(self, context_id: str) -> None:
        """Remove model override for the given context."""


class SessionModelContextService(SessionModelContextInterface):
    """Persisted per-chat model override storage."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self._storage_path = storage_path
        self._model_overrides = self._load()

    def get_model_override(self, context_id: str) -> str | None:
        value = self._model_overrides.get(context_id.strip())
        if value is None:
            return None
        normalized = value.strip()
        return normalized if normalized else None

    def set_model_override(self, context_id: str, model_id: str) -> None:
        normalized_context = context_id.strip()
        normalized_model = model_id.strip()
        if not normalized_context or not normalized_model:
            return
        self._model_overrides[normalized_context] = normalized_model
        self._save()

    def clear_model_override(self, context_id: str) -> None:
        normalized_context = context_id.strip()
        if not normalized_context:
            return
        self._model_overrides.pop(normalized_context, None)
        self._save()

    def _load(self) -> dict[str, str]:
        path = self._storage_path
        if path is None or not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("model_context.load_failed", path=str(path))
            return {}
        if not isinstance(raw, dict):
            return {}
        loaded: dict[str, str] = {}
        for ctx_id, model_id in raw.items():
            if isinstance(ctx_id, str) and isinstance(model_id, str):
                ctx_clean = ctx_id.strip()
                model_clean = model_id.strip()
                if ctx_clean and model_clean:
                    loaded[ctx_clean] = model_clean
        return loaded

    def _save(self) -> None:
        path = self._storage_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(self._model_overrides)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(encoded)
            tmp_path.replace(path)
        except OSError:
            logger.warning("model_context.save_failed", path=str(path))
