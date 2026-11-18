"""Test helpers: in-memory capability snapshots and catalogs (no manifest files)."""

from collections.abc import Mapping
from pathlib import Path

from assistant.agent.tools.registry import ActiveCapabilities, active_capabilities_for
from assistant.core.capabilities.interfaces import CapabilityCatalogInterface, SnapshotSubscriber
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue, CapabilitySnapshot
from assistant.core.config.schemas import RuntimeConfig


def snapshot_of(
    definitions: Mapping[str, CapabilityDefinition],
    revision: int = 1,
    errors: tuple[CapabilityLoadIssue, ...] = (),
) -> CapabilitySnapshot:
    """A snapshot over ``definitions`` without backing files."""
    return CapabilitySnapshot(
        revision=revision,
        definitions=definitions,
        files={},
        capabilities_dir=Path("/nonexistent/capabilities"),
        errors=errors,
    )


def active_for(
    config: RuntimeConfig, definitions: Mapping[str, CapabilityDefinition]
) -> ActiveCapabilities:
    """``config``'s policy resolved against ``definitions``."""
    return active_capabilities_for(config, snapshot_of(definitions))


class StaticCatalog(CapabilityCatalogInterface):
    """Catalog serving a snapshot that tests replace explicitly."""

    def __init__(self, snapshot: CapabilitySnapshot) -> None:
        self.snapshot = snapshot
        self.refresh_calls = 0
        self._subscribers: list[SnapshotSubscriber] = []

    def current(self) -> CapabilitySnapshot:
        return self.snapshot

    def refresh(self) -> CapabilitySnapshot:
        self.refresh_calls += 1
        return self.snapshot

    def reload(self, reason: str) -> CapabilitySnapshot:
        return self.refresh()

    def subscribe(self, callback: SnapshotSubscriber) -> None:
        self._subscribers.append(callback)
