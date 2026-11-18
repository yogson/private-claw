"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Cache of PydanticAITurnAdapter instances keyed by capability policy and snapshot revision.

Avoids rebuilding the system prompt and tool list on every turn when a
per-session capability override is active (Option C — per-session adapter
hot-swap).  Adapters are lazily built on first use and reused for all
subsequent turns that share the same capability set and manifest revision.
"""

from collections import OrderedDict

import structlog

from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from assistant.core.config.schemas import RuntimeConfig

logger = structlog.get_logger(__name__)

type AdapterCacheKey = tuple[frozenset[str], frozenset[str], int]

DEFAULT_MAX_ADAPTERS = 16


class TurnAdapterCache:
    """Cache of :class:`PydanticAITurnAdapter` instances.

    The key is ``(frozenset(enabled), frozenset(denied), snapshot.revision)``: the raw
    (unexpanded) policy plus the capability snapshot revision. The same policy against
    the same revision always resolves to the same prompt and tools, so such configs share
    an entry; a manifest change (new revision) or a different ``denied`` list builds a
    new adapter.

    Memory stays bounded: when a newer revision is first requested, all adapters of
    older revisions are evicted, and the cache never holds more than ``max_size``
    adapters (least recently used first out). A request for a revision older than the
    newest one seen (a turn that took its snapshot just before a swap) gets a freshly
    built, uncached adapter.

    The default capability set is pre-populated in ``__init__`` so the very first turn
    never pays a build cost.

    MCP tools need no invalidation here: adapters hold shared per-server MCP connections,
    which list tools per run, refresh their own cache on ``tools/list_changed`` and are
    reconfigured in place (filters / confirm_tools read live from the connection) when the
    server's config changes.

    Thread-safety: not required — the application runs on a single asyncio
    event loop and adapters are built synchronously.
    """

    def __init__(
        self,
        model_id: str,
        max_tokens: int,
        base_config: RuntimeConfig,
        snapshot: CapabilitySnapshot,
        max_size: int = DEFAULT_MAX_ADAPTERS,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._max_size = max_size
        self._cache: OrderedDict[AdapterCacheKey, PydanticAITurnAdapter] = OrderedDict()
        self._latest_revision = snapshot.revision

        # Pre-warm with the default capability set so the first default turn
        # never incurs a build cost.
        default_key = self._make_key(base_config, snapshot)
        self._cache[default_key] = self._build(base_config, snapshot)
        logger.debug(
            "adapter_cache.initialized",
            capability_key=sorted(default_key[0]),
            revision=snapshot.revision,
            model_id=model_id,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_build(
        self, config: RuntimeConfig, snapshot: CapabilitySnapshot
    ) -> PydanticAITurnAdapter:
        """Return a cached adapter for *config*'s capability policy at *snapshot*'s revision.

        On a cache miss the adapter is built synchronously and stored for future use.
        """
        key = self._make_key(config, snapshot)
        adapter = self._cache.get(key)
        if adapter is not None:
            self._cache.move_to_end(key)
            return adapter
        if snapshot.revision < self._latest_revision:
            logger.info(
                "adapter_cache.stale_revision",
                revision=snapshot.revision,
                latest_revision=self._latest_revision,
            )
            return self._build(config, snapshot)
        if snapshot.revision > self._latest_revision:
            self._evict_older_than(snapshot.revision)
        logger.info(
            "adapter_cache.miss",
            capability_key=sorted(key[0]),
            denied_key=sorted(key[1]),
            revision=snapshot.revision,
            cache_size=len(self._cache),
        )
        adapter = self._build(config, snapshot)
        self._cache[key] = adapter
        while len(self._cache) > self._max_size:
            evicted, _ = self._cache.popitem(last=False)
            logger.debug("adapter_cache.evicted_lru", capability_key=sorted(evicted[0]))
        return adapter

    @property
    def size(self) -> int:
        """Number of adapters currently cached."""
        return len(self._cache)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_older_than(self, revision: int) -> None:
        stale = [key for key in self._cache if key[2] < revision]
        for key in stale:
            del self._cache[key]
        logger.info(
            "adapter_cache.revision_changed",
            previous_revision=self._latest_revision,
            revision=revision,
            evicted=len(stale),
        )
        self._latest_revision = revision

    def _build(self, config: RuntimeConfig, snapshot: CapabilitySnapshot) -> PydanticAITurnAdapter:
        return PydanticAITurnAdapter(
            model_id=self._model_id,
            max_tokens=self._max_tokens,
            config=config,
            snapshot=snapshot,
        )

    @staticmethod
    def _make_key(config: RuntimeConfig, snapshot: CapabilitySnapshot) -> AdapterCacheKey:
        """Derive a stable, hashable cache key from the raw policy and the revision."""
        policy = config.capabilities
        return (
            frozenset(policy.enabled_capabilities),
            frozenset(policy.denied_capabilities),
            snapshot.revision,
        )
