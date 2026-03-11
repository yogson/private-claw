"""
Component ID: CMP_STORE_IDEMPOTENCY_LEDGER

Ingress idempotency service for channel and API duplicate prevention.
Wraps IdempotencyLedgerInterface with a domain-oriented API for ingress paths.
"""

from assistant.store.interfaces import IdempotencyKeyExistsError, IdempotencyLedgerInterface
from assistant.store.models import IdempotencyRecord


class DuplicateIngressError(Exception):
    """Raised when an ingress event with the same key has already been registered."""

    def __init__(self, key: str, prior: IdempotencyRecord | None) -> None:
        super().__init__(f"Duplicate ingress event: {key}")
        self.key = key
        self.prior = prior


class IngressIdempotencyService:
    """
    Domain service for ingress idempotency registration and duplicate detection.

    Used by channel adapters (Telegram, API) to deduplicate incoming events
    before they are dispatched to the orchestrator.

    Keys are namespaced as ``<source>:<event_id>`` to prevent cross-source collisions.
    """

    def __init__(
        self,
        ledger: IdempotencyLedgerInterface,
        default_ttl_seconds: int = 86400,
    ) -> None:
        self._ledger = ledger
        self._default_ttl_seconds = default_ttl_seconds

    def build_key(self, source: str, event_id: str) -> str:
        """Build a canonical idempotency key from source and event ID."""
        return f"{source}:{event_id}"

    async def is_duplicate(self, source: str, event_id: str) -> bool:
        """
        Check whether an ingress event has already been registered.

        Does not register the key; use ``check_and_register`` for atomic guard.
        Returns True if the key exists and is not expired.
        """
        key = self.build_key(source, event_id)
        record = await self._ledger.check(key)
        return record is not None

    async def register(
        self,
        source: str,
        event_id: str,
        ttl_seconds: int | None = None,
    ) -> IdempotencyRecord:
        """
        Register an ingress event key.

        Raises DuplicateIngressError if the key is already registered and not expired.
        """
        key = self.build_key(source, event_id)
        try:
            return await self._ledger.register(key, source, self._resolve_ttl(ttl_seconds))
        except IdempotencyKeyExistsError as exc:
            prior = await self._ledger.check(key)
            raise DuplicateIngressError(key, prior) from exc

    async def check_and_register(
        self,
        source: str,
        event_id: str,
        ttl_seconds: int | None = None,
    ) -> tuple[bool, IdempotencyRecord | None]:
        """
        Atomically check for a duplicate and register if new.

        Returns ``(is_duplicate, prior_record)``.
        - If ``is_duplicate`` is True, ``prior_record`` is the existing registration.
        - If ``is_duplicate`` is False, the key has been registered and ``prior_record`` is None.
        """
        key = self.build_key(source, event_id)
        return await self._ledger.check_and_register(key, source, self._resolve_ttl(ttl_seconds))

    def _resolve_ttl(self, ttl_seconds: int | None) -> int:
        return ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds
