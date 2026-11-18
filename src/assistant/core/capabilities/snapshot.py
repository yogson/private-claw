"""
Component ID: CMP_CORE_CAPABILITIES

Immutable, versioned view of the capability manifests served by the catalog.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from assistant.core.capabilities.schemas import CapabilityDefinition

type IssueKind = Literal["parse", "validation", "warning"]


@dataclass(frozen=True)
class ManifestFileState:
    """Where a capability came from: file path, stat signature and content hash.

    ``sha256`` is the hash of the raw file bytes; it is the precondition an editor (#54)
    compares against before writing.
    """

    path: Path
    mtime_ns: int
    size: int
    sha256: str


@dataclass(frozen=True)
class CapabilityLoadIssue:
    """A problem found while (re)loading manifests.

    ``parse`` and ``validation`` issues reject a reload (the last-known-good snapshot stays
    current); ``warning`` issues are informational and do not block a swap.
    """

    kind: IssueKind
    message: str
    file: str | None = None

    @property
    def blocking(self) -> bool:
        """True when the issue rejected the reload."""
        return self.kind != "warning"


def _frozen[K, V](mapping: Mapping[K, V]) -> Mapping[K, V]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class CapabilitySnapshot:
    """One consistent state of all capability manifests.

    ``revision`` grows by one on every successful swap, so it identifies the manifest
    content a turn ran with. ``definitions`` and ``files`` are read-only mappings keyed by
    ``capability_id``; the definitions themselves are frozen pydantic models. ``errors``
    lists the problems of the most recent reload attempt: while it holds blocking issues
    the catalog keeps serving this (last-known-good) revision.
    """

    revision: int
    definitions: Mapping[str, CapabilityDefinition]
    files: Mapping[str, ManifestFileState]
    capabilities_dir: Path
    errors: tuple[CapabilityLoadIssue, ...] = ()
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "definitions", _frozen(self.definitions))
        object.__setattr__(self, "files", _frozen(self.files))

    def get(self, capability_id: str) -> CapabilityDefinition | None:
        """Return the definition for ``capability_id`` or None."""
        return self.definitions.get(capability_id)

    def file_for(self, capability_id: str) -> ManifestFileState | None:
        """Return the manifest file state ``capability_id`` was loaded from, or None."""
        return self.files.get(capability_id)

    @property
    def blocking_errors(self) -> tuple[CapabilityLoadIssue, ...]:
        """Issues that rejected the latest reload (empty when the snapshot is current)."""
        return tuple(issue for issue in self.errors if issue.blocking)

    def file_hashes(self, capability_ids: Iterable[str]) -> dict[str, str]:
        """``capability_id -> sha256`` for the given ids that have a manifest file."""
        return {
            cap_id: state.sha256
            for cap_id in capability_ids
            if (state := self.files.get(cap_id)) is not None
        }
