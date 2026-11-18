"""
Component ID: CMP_CORE_CAPABILITIES

Incremental reader of ``<config_dir>/capabilities/*.yaml`` for the catalog.

``scan()`` returns the cheap stat signature of the directory; ``build()`` turns a scan
into a validated candidate, re-reading only files whose stat changed and re-parsing only
files whose content hash changed.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from assistant.core.capabilities.loader import (
    ManifestParseError,
    discover_capability_manifests,
    parse_capability_manifest,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilityLoadIssue, ManifestFileState
from assistant.core.capabilities.validation import CapabilityValidator, validate_nested_references

type ManifestParser = Callable[[str, bytes], CapabilityDefinition]
type StatSignature = tuple[tuple[str, int, int], ...]
type ScannedFile = tuple[Path, int, int]


@dataclass(frozen=True)
class _ParsedFile:
    state: ManifestFileState
    definition: CapabilityDefinition | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestCandidate:
    """A parsed and validated set of manifests that may become the next snapshot."""

    issues: tuple[CapabilityLoadIssue, ...]
    definitions: dict[str, CapabilityDefinition]
    states: dict[str, ManifestFileState]
    content_key: frozenset[tuple[str, str]]

    @property
    def blocking(self) -> bool:
        """True when any issue rejects this candidate."""
        return any(issue.blocking for issue in self.issues)


class ManifestScanner:
    """Stat-based, hash-checked incremental manifest reader (keeps a per-file parse cache)."""

    def __init__(
        self,
        capabilities_dir: Path,
        *,
        validator: CapabilityValidator | None = None,
        parser: ManifestParser = parse_capability_manifest,
    ) -> None:
        self._dir = capabilities_dir
        self._validator = validator
        self._parser = parser
        self._parsed: dict[str, _ParsedFile] = {}

    def scan(self) -> tuple[StatSignature, list[ScannedFile]]:
        """Return ``(signature, files)``: sorted ``(name, mtime_ns, size)`` of each manifest."""
        entries: list[ScannedFile] = []
        for path in discover_capability_manifests(self._dir):
            try:
                st = path.stat()
            except OSError:
                continue  # removed between listing and stat; the next refresh settles it
            entries.append((path, st.st_mtime_ns, st.st_size))
        signature = tuple((path.name, mtime_ns, size) for path, mtime_ns, size in entries)
        return signature, entries

    def build(self, files: list[ScannedFile], *, force: bool = False) -> ManifestCandidate:
        """Parse ``files`` (cached unless ``force``) and run the cross-file validation."""
        parsed_files = tuple(self._parse(path, m, s, force) for path, m, s in files)
        self._parsed = {f.state.path.name: f for f in parsed_files}
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
            issues.extend(self._validate(definitions))
        content_key = frozenset((f.state.path.name, f.state.sha256) for f in parsed_files)
        return ManifestCandidate(tuple(issues), definitions, states, content_key)

    def _validate(self, definitions: dict[str, CapabilityDefinition]) -> list[CapabilityLoadIssue]:
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
            and (cached.state.mtime_ns, cached.state.size) == (mtime_ns, size)
        ):
            return cached
        try:
            raw = path.read_bytes()
        except OSError as exc:
            state = ManifestFileState(path=path, mtime_ns=mtime_ns, size=size, sha256="")
            return _ParsedFile(state, None, (f"[{path.name}] read error: {exc}",))
        state = ManifestFileState(
            path=path, mtime_ns=mtime_ns, size=size, sha256=hashlib.sha256(raw).hexdigest()
        )
        if cached is not None and cached.state.sha256 == state.sha256:
            return replace(cached, state=state)  # touch-only change: no re-parse
        try:
            return _ParsedFile(state, self._parser(path.name, raw))
        except ManifestParseError as exc:
            return _ParsedFile(state, None, tuple(exc.messages))
        except Exception as exc:  # parser bug: never let it escape into a turn
            return _ParsedFile(state, None, (f"[{path.name}] unexpected parse error: {exc}",))
