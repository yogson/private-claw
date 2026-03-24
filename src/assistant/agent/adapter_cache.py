"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Cache of PydanticAITurnAdapter instances keyed by capability set.

Avoids rebuilding the system prompt and tool list on every turn when a
per-session capability override is active (Option C — per-session adapter
hot-swap).  Adapters are lazily built on first use and reused for all
subsequent turns that share the same capability set.
"""

import structlog

from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.core.config.schemas import RuntimeConfig

logger = structlog.get_logger(__name__)


class TurnAdapterCache:
    """Cache of :class:`PydanticAITurnAdapter` instances keyed by capability set.

    The cache key is ``frozenset(config.capabilities.enabled_capabilities)``
    — the *raw* (unexpanded) enabled list.  Two configs that share the same
    ``enabled_capabilities`` list always produce the same expanded capability
    set, system prompt, and tool set, so they correctly share a cache entry.

    The default capability set is pre-populated in ``__init__`` so the very
    first turn never pays a build cost.  Subsequent turns with a different
    capability override pay the build cost once; all later turns reuse the
    cached adapter.

    Thread-safety: not required — the application runs on a single asyncio
    event loop and adapters are built synchronously.
    """

    def __init__(
        self,
        model_id: str,
        max_tokens: int,
        base_config: RuntimeConfig,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._cache: dict[frozenset[str], PydanticAITurnAdapter] = {}

        # Pre-warm with the default capability set so the first default turn
        # never incurs a build cost.
        default_key = self._make_key(base_config)
        self._cache[default_key] = PydanticAITurnAdapter(
            model_id=model_id,
            max_tokens=max_tokens,
            config=base_config,
        )
        logger.debug(
            "adapter_cache.initialized",
            capability_key=sorted(default_key),
            model_id=model_id,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_build(self, config: RuntimeConfig) -> PydanticAITurnAdapter:
        """Return a cached adapter for *config*'s capability set.

        On a cache miss the adapter is built synchronously and stored for
        future use.  The default capability set is pre-populated at
        construction time, so the miss path is only exercised when a new
        capability combination is encountered for the first time (e.g. when
        a user switches to a session with a different capability override).
        """
        key = self._make_key(config)
        if key not in self._cache:
            logger.info(
                "adapter_cache.miss",
                capability_key=sorted(key),
                cache_size=len(self._cache),
            )
            self._cache[key] = PydanticAITurnAdapter(
                model_id=self._model_id,
                max_tokens=self._max_tokens,
                config=config,
            )
        return self._cache[key]

    @property
    def size(self) -> int:
        """Number of distinct capability sets currently cached."""
        return len(self._cache)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_key(config: RuntimeConfig) -> frozenset[str]:
        """Derive a stable, hashable cache key from the raw enabled-capabilities list."""
        return frozenset(config.capabilities.enabled_capabilities)
