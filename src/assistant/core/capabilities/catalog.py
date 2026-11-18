"""
Component ID: CMP_CORE_CAPABILITIES

CapabilityCatalog: owns the capability manifests as an immutable, versioned snapshot.

Change detection is a cheap stat signature (file name, ``st_mtime_ns``, ``st_size``) of
``<config_dir>/capabilities/*.yaml``, checked by :meth:`CapabilityCatalog.refresh` at turn
start. Only files whose stat changed are re-read (plus "racily clean" files modified
within the last ~2 s, and unreadable files), and a content hash keeps touch-only changes
from bumping the revision. A reload that fails (YAML, schema or cross-file
validation) keeps the whole last-known-good snapshot and records the issues on it.
"""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import structlog

from assistant.core.capabilities.interfaces import CapabilityCatalogInterface, SnapshotSubscriber
from assistant.core.capabilities.loader import CapabilityLoadError, parse_capability_manifest
from assistant.core.capabilities.manifest_scanner import (
    ManifestParser,
    ManifestScanner,
    ScannedFile,
    StatSignature,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue, CapabilitySnapshot
from assistant.core.capabilities.validation import CapabilityValidator
from assistant.core.config.loader import resolve_config_dir

logger = structlog.get_logger(__name__)

DEFAULT_MIN_REFRESH_INTERVAL_SECONDS = 1.0


class CapabilityCatalog(CapabilityCatalogInterface):
    """Single owner of capability manifest state (see module docstring).

    The initial load in ``__init__`` is fail-fast: blocking issues raise
    :class:`CapabilityLoadError`, because there is no last-known-good state yet.
    ``validator`` adds cross-file checks (e.g. tool bindings against tools.yaml) on top of
    the built-in duplicate-id and nested-reference checks.

    Thread-safety: refresh/reload are serialized by a lock; readers get the current
    snapshot through a single attribute read (atomic swap).
    """

    def __init__(
        self,
        config_dir: str | Path | None = None,
        *,
        validator: CapabilityValidator | None = None,
        min_refresh_interval: float = DEFAULT_MIN_REFRESH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        parser: ManifestParser = parse_capability_manifest,
    ) -> None:
        self._capabilities_dir = resolve_config_dir(config_dir) / "capabilities"
        self._scanner = ManifestScanner(self._capabilities_dir, validator=validator, parser=parser)
        self._min_refresh_interval = min_refresh_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._subscribers: list[SnapshotSubscriber] = []
        self._reported: frozenset[str] = frozenset()
        self._blocked = False

        signature, files = self._scanner.scan()
        candidate = self._scanner.build(files, force=True)
        blocking = [i.message for i in candidate.issues if i.blocking]
        if blocking:
            raise CapabilityLoadError(
                "Capability manifest validation failed:\n"
                + "\n".join(f"  - {message}" for message in blocking)
            )
        # None forces the next refresh past the signature check (see ManifestCandidate.retry).
        self._signature: StatSignature | None = None if candidate.retry else signature
        self._content_key = candidate.content_key
        self._last_check = self._clock()
        self._snapshot = CapabilitySnapshot(
            revision=1,
            definitions=candidate.definitions,
            files=candidate.states,
            capabilities_dir=self._capabilities_dir,
            errors=candidate.issues,
        )
        self._report(self._snapshot)

    @property
    def capabilities_dir(self) -> Path:
        """Directory the manifests are read from (``<config_dir>/capabilities``)."""
        return self._capabilities_dir

    def current(self) -> CapabilitySnapshot:
        """Return the current snapshot without any I/O."""
        return self._snapshot

    def refresh(self) -> CapabilitySnapshot:
        """Re-parse and swap when the stat signature changed; otherwise return as is."""
        with self._lock:
            now = self._clock()
            if now - self._last_check < self._min_refresh_interval:
                return self._snapshot
            self._last_check = now
            return self._guarded(force=False, reason="change_detected")

    def reload(self, reason: str) -> CapabilitySnapshot:
        """Re-read every manifest regardless of the stat signature."""
        with self._lock:
            self._last_check = self._clock()
            return self._guarded(force=True, reason=reason)

    def subscribe(self, callback: SnapshotSubscriber) -> None:
        """Register ``callback`` for successful swaps to a new revision."""
        self._subscribers.append(callback)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _guarded(self, *, force: bool, reason: str) -> CapabilitySnapshot:
        # Turns call refresh(): no manifest or filesystem problem may escape from here.
        try:
            signature, files = self._scanner.scan()
            if not force and signature == self._signature:
                return self._snapshot
            return self._apply(signature, files, force=force, reason=reason)
        except Exception as exc:
            issue = CapabilityLoadIssue("validation", f"refresh failed: {exc}")
            self._signature = None  # retry on the next refresh
            if self._snapshot.errors != (issue,):
                self._snapshot = replace(self._snapshot, errors=(issue,))
            self._report(self._snapshot, exc_info=exc)  # traceback once per distinct error
            return self._snapshot

    def _apply(
        self,
        signature: StatSignature,
        files: list[ScannedFile],
        *,
        force: bool,
        reason: str,
    ) -> CapabilitySnapshot:
        candidate = self._scanner.build(files, force=force)
        self._signature = None if candidate.retry else signature
        current = self._snapshot
        if candidate.blocking or candidate.content_key == self._content_key:
            # Rejected (LKG) or same content: keep the revision, update only the errors.
            if current.errors != candidate.issues:
                self._snapshot = replace(current, errors=candidate.issues)
        else:
            self._content_key = candidate.content_key
            self._snapshot = CapabilitySnapshot(
                revision=current.revision + 1,
                definitions=candidate.definitions,
                files=candidate.states,
                capabilities_dir=self._capabilities_dir,
                errors=candidate.issues,
            )
            logger.info(
                "capabilities.reloaded",
                revision=self._snapshot.revision,
                previous_revision=current.revision,
                reason=reason,
                changed=_changed_ids(current.definitions, candidate.definitions),
            )
        self._report(self._snapshot)
        if self._snapshot.revision != current.revision:
            self._notify(self._snapshot)
        return self._snapshot

    def _report(self, snapshot: CapabilitySnapshot, exc_info: BaseException | None = None) -> None:
        messages = frozenset(issue.message for issue in snapshot.errors)
        extra = {"exc_info": exc_info} if exc_info is not None else {}
        for issue in snapshot.errors:
            if issue.message in self._reported:
                continue
            logger.warning(
                "capabilities.reload_failed" if issue.blocking else "capabilities.reload_warning",
                file=issue.file,
                error=issue.message,
                kind=issue.kind,
                lkg_revision=snapshot.revision,
                **extra,
            )
        blocked = bool(snapshot.blocking_errors)
        if self._blocked and not blocked:
            logger.info("capabilities.reload_recovered", revision=snapshot.revision)
        self._blocked = blocked
        self._reported = messages

    def _notify(self, snapshot: CapabilitySnapshot) -> None:
        for callback in list(self._subscribers):
            try:
                callback(snapshot)
            except Exception:
                logger.exception(
                    "capabilities.subscriber_failed",
                    revision=snapshot.revision,
                    subscriber=getattr(callback, "__qualname__", repr(callback)),
                )


def _changed_ids(
    before: Mapping[str, CapabilityDefinition], after: Mapping[str, CapabilityDefinition]
) -> list[str]:
    return sorted(
        cap_id for cap_id in before.keys() | after.keys() if before.get(cap_id) != after.get(cap_id)
    )
