"""
Component ID: CMP_CORE_CAPABILITIES

Incremental reader of ``<config_dir>/capabilities/*.yaml`` for the catalog.

``scan()`` returns the cheap stat signature of the directory; ``build()`` turns a scan
into a validated candidate, re-reading only files whose stat changed and re-parsing only
files whose content hash changed.

"Racily clean" files (Git's rule): a file whose mtime is within ``RACY_WINDOW_NS`` of the
moment it was read may still be modified within the same mtime tick without changing its
stat (coarse-granularity filesystems, scripted edits right after a turn). Such a file is
re-hashed on every build until its mtime is older than the read; the hash check keeps this
from bumping the revision.
"""

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import structlog

from assistant.core.capabilities.loader import (
    ManifestParseError,
    discover_capability_manifests,
    parse_capability_manifest,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue, ManifestFileState
from assistant.core.capabilities.validation import CapabilityValidator, validate_nested_references

logger = structlog.get_logger(__name__)

type ManifestParser = Callable[[str, bytes], CapabilityDefinition]
type StatSignature = tuple[tuple[str, int, int], ...]
type ScannedFile = tuple[Path, int, int]

# Covers 1-2 s mtime granularity (HFS+, FAT/exFAT, some SMB/NFS mounts).
RACY_WINDOW_NS = 2_000_000_000


@dataclass(frozen=True)
class _ParsedFile:
    state: ManifestFileState
    definition: CapabilityDefinition | None
    errors: tuple[str, ...] = ()
    retry: bool = False  # not cacheable (read error): re-read on every build
    racy: bool = False  # cached, but the stat cannot vouch for it yet: re-hash on next build


@dataclass(frozen=True)
class ManifestCandidate:
    """A parsed and validated set of manifests that may become the next snapshot."""

    issues: tuple[CapabilityLoadIssue, ...]
    definitions: dict[str, CapabilityDefinition]
    states: dict[str, ManifestFileState]
    content_key: frozenset[tuple[str, str]]
    retry: bool = False
    """True when some file must be re-read on the next refresh even if its stat is unchanged
    (a read error, or a racily clean file)."""

    @property
    def blocking(self) -> bool:
        """True when any issue rejects this candidate."""
        return any(issue.blocking for issue in self.issues)


def describe_os_error(exc: OSError, *, file_names: bool = True) -> str:
    """``exc`` for the owner-facing issue text: the OS message plus file base names only.

    ``str(exc)`` carries absolute paths (home directory, user name), which would reach the
    Telegram ``/capabilities`` view. An error without ``strerror`` has no file name set and
    is shown as is.
    """
    if not exc.strerror:
        return str(exc) or type(exc).__name__
    names = [
        repr(os.path.basename(os.fsdecode(name)))
        for name in (exc.filename, exc.filename2)
        if file_names and isinstance(name, str | bytes | os.PathLike)
    ]
    return ": ".join([exc.strerror, *names])


class ManifestScanner:
    """Stat-based, hash-checked incremental manifest reader (keeps a per-file parse cache)."""

    def __init__(
        self,
        capabilities_dir: Path,
        *,
        validator: CapabilityValidator | None = None,
        parser: ManifestParser = parse_capability_manifest,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._dir = capabilities_dir
        self._validator = validator
        self._parser = parser
        self._now_ns = now_ns
        self._parsed: dict[str, _ParsedFile] = {}

    def scan(self) -> tuple[StatSignature, list[ScannedFile]]:
        """Return ``(signature, files)``: sorted ``(name, mtime_ns, size)`` of each manifest."""
        entries: list[ScannedFile] = []
        for path in discover_capability_manifests(self._dir):
            try:
                st = path.stat()
            except OSError as exc:
                # Removed between listing and stat (the next refresh settles it), or a
                # dangling symlink: either way the file contributes no capability.
                logger.debug("capabilities.manifest_skipped", file=path.name, error=str(exc))
                continue
            entries.append((path, st.st_mtime_ns, st.st_size))
        signature = tuple((path.name, mtime_ns, size) for path, mtime_ns, size in entries)
        return signature, entries

    def build(self, files: list[ScannedFile], *, force: bool = False) -> ManifestCandidate:
        """Parse ``files`` (cached unless ``force``) and run the cross-file validation."""
        parsed_files = tuple(self._parse(path, m, s, force) for path, m, s in files)
        self._parsed = {f.state.path.name: f for f in parsed_files if not f.retry}
        issues: list[CapabilityLoadIssue] = []
        definitions: dict[str, CapabilityDefinition] = {}
        states: dict[str, ManifestFileState] = {}
        for parsed in parsed_files:
            name = parsed.state.path.name
            issues.extend(CapabilityLoadIssue("parse", m, file=name) for m in parsed.errors)
            definition = parsed.definition
            if definition is None:
                continue
            if definition.capability_id in definitions:
                message = f"[{name}] duplicate capability_id: {definition.capability_id}"
                issues.append(CapabilityLoadIssue("validation", message, file=name))
                continue
            definitions[definition.capability_id] = definition
            states[definition.capability_id] = parsed.state
        if not issues:
            issues.extend(self.validate(definitions))
        content_key = frozenset((f.state.path.name, f.state.sha256) for f in parsed_files)
        retry = any(f.retry or f.racy for f in parsed_files)
        return ManifestCandidate(tuple(issues), definitions, states, content_key, retry)

    def validate(self, definitions: dict[str, CapabilityDefinition]) -> list[CapabilityLoadIssue]:
        """Cross-file validation of ``definitions`` (nested references + the validator)."""
        issues = validate_nested_references(definitions)
        if self._validator is not None:
            try:
                issues.extend(self._validator(definitions))
            except Exception as exc:
                issues.append(CapabilityLoadIssue("validation", f"validator failed: {exc}"))
        return issues

    def _parse(self, path: Path, mtime_ns: int, size: int, force: bool) -> _ParsedFile:
        cached = self._parsed.get(path.name)
        if (
            not force
            and cached is not None
            and not cached.racy
            and (cached.state.mtime_ns, cached.state.size) == (mtime_ns, size)
        ):
            return cached
        racy = mtime_ns >= self._now_ns() - RACY_WINDOW_NS
        try:
            raw = path.read_bytes()
        except OSError as exc:
            state = ManifestFileState(path=path, mtime_ns=mtime_ns, size=size, sha256="")
            # Not cached: fixing the cause (chmod, remount) changes neither mtime nor size.
            reason = describe_os_error(exc, file_names=False)  # the name is in the prefix
            message = f"[{path.name}] read error: {reason}"
            return _ParsedFile(state, None, (message,), retry=True)
        state = ManifestFileState(
            path=path, mtime_ns=mtime_ns, size=size, sha256=hashlib.sha256(raw).hexdigest()
        )
        if cached is not None and cached.state.sha256 == state.sha256:
            return replace(cached, state=state, racy=racy)  # touch-only change: no re-parse
        try:
            return _ParsedFile(state, self._parser(path.name, raw), racy=racy)
        except ManifestParseError as exc:
            return _ParsedFile(state, None, tuple(exc.messages), racy=racy)
        except Exception as exc:  # parser bug: never let it escape into a turn
            message = f"[{path.name}] unexpected parse error: {exc}"
            return _ParsedFile(state, None, (message,), racy=racy)
