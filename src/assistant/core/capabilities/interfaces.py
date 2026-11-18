"""
Component ID: CMP_CORE_CAPABILITIES

Interface of the capability catalog, the single owner of capability manifest state.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable

from assistant.core.capabilities.snapshot import CapabilitySnapshot

type SnapshotSubscriber = Callable[[CapabilitySnapshot], None]


class CapabilityCatalogInterface(ABC):
    """Serves immutable capability snapshots and picks up manifest changes live."""

    @abstractmethod
    def current(self) -> CapabilitySnapshot:
        """Return the current snapshot without any I/O."""

    @abstractmethod
    def refresh(self) -> CapabilitySnapshot:
        """Check the manifests for changes (cheap stat signature) and return the snapshot.

        Re-parses only when the signature changed; a broken result keeps the
        last-known-good snapshot and records the problem in ``snapshot.errors``.
        """

    @abstractmethod
    def reload(self, reason: str) -> CapabilitySnapshot:
        """Force a re-read of every manifest (e.g. right after an approved write)."""

    @abstractmethod
    def subscribe(self, callback: SnapshotSubscriber) -> None:
        """Call ``callback(snapshot)`` after every successful swap to a new revision.

        Callbacks run after the catalog's lock is released, so they may call back into the
        catalog; their exceptions are logged and never raised.
        """
