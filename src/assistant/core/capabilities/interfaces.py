"""
Component ID: CMP_CORE_CAPABILITIES

Interface of the capability catalog, the single owner of capability manifest state.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping

from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue, CapabilitySnapshot
from assistant.core.capabilities.validation import validate_nested_references

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

        Callbacks run after the catalog's lock is released, one delivery at a time: each
        callback receives a revision at most once, never an older revision after a newer
        one, and always ends on the latest revision (intermediate revisions may be
        skipped). Callbacks may call back into the catalog; a revision produced meanwhile
        (by a callback or another thread) is delivered by the delivery already running, so
        that ``refresh()``/``reload()`` may return before its revision reached every
        callback. Callback exceptions are logged and never raised.
        """

    def validate_candidate(
        self, definitions: Mapping[str, CapabilityDefinition]
    ) -> list[CapabilityLoadIssue]:
        """Run the cross-file checks a reload would run on ``definitions`` (no I/O, no swap).

        Lets an editor reject a change before writing it. The default checks nested
        references only; :class:`CapabilityCatalog` adds its configured validator.
        """
        return validate_nested_references(definitions)
