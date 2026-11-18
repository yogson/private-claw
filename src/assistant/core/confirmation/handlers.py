"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

Per-kind confirmation handler contract and registry.
"""

from typing import Protocol

from assistant.core.confirmation.models import ConfirmationOutcome, ConfirmationRequest


class ConfirmationHandlerInterface(Protocol):
    """Applies an approved confirmation of one kind."""

    @property
    def kind(self) -> str:
        """Confirmation kind this handler applies (e.g. 'memory_update')."""
        ...

    async def apply(
        self, request: ConfirmationRequest, *, user_id: str | None
    ) -> ConfirmationOutcome:
        """Apply the approved request; must not raise for expected domain failures."""
        ...


class ConfirmationHandlerRegistry:
    """Maps confirmation kinds to their handlers."""

    def __init__(self, handlers: list[ConfirmationHandlerInterface] | None = None) -> None:
        self._handlers: dict[str, ConfirmationHandlerInterface] = {}
        for handler in handlers or []:
            self.register(handler)

    def register(self, handler: ConfirmationHandlerInterface) -> None:
        """Register a handler; a later registration for the same kind replaces the earlier."""
        self._handlers[handler.kind] = handler

    def get(self, kind: str) -> ConfirmationHandlerInterface | None:
        """Return the handler for kind, or None if the kind is unknown."""
        return self._handlers.get(kind)
