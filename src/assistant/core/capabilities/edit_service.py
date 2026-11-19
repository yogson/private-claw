"""
Component ID: CMP_CORE_CAPABILITIES

CapabilityEditService: agent-proposed edits of capability manifests (kind ``capability_edit``).

All domain logic of manifest self-editing lives here; the agent tools and the confirmation
handler are thin wrappers around it.

- Only manifests a human opted in with ``self_edit: true`` are editable, and only when the
  file (after following symlinks) lives inside the active config dir.
- Edits are exact text replacements on the raw file, so comments and formatting outside the
  edited span stay byte-identical. The result must parse and validate (strict schema plus the
  catalog's cross-file checks) before it is proposed and again before it is applied.
- The agent can change neither ``capability_id`` (that would be a rename, i.e. create +
  delete) nor ``self_edit`` (only a human sets or clears it). This is enforced on the YAML
  node tree (``manifest_structure``: exactly one of each top-level key, no duplicate or
  merge keys, no aliases, ``self_edit`` is the plain boolean ``true``) and again on the
  parsed result, never by text matching: YAML escapes and merge keys hide keys from text.
- ``tool_overrides`` keys and tool bindings added by an edit must name tools from the
  tools registry (``tools.yaml``) when the service knows it.
- Every proposal carries the sha256 of the raw bytes it was computed from. Approval re-checks
  it, so a proposal that is stale because the file changed in between is never applied.
- Before every applied edit the current bytes are backed up to
  ``<config_dir>/.capability_history/<capability_id>/<history_id>.yaml`` with a ``.json``
  sidecar (version history and rollback). The history lives outside ``capabilities/``, so the
  catalog never loads it as a manifest.
- The new content is written atomically (temp file in the target's directory, fsync,
  ``os.replace``; file mode kept; a symlinked manifest is written at its target), then
  ``catalog.reload()`` makes the next turn use the new revision. If the catalog rejects the
  result, the previous content is restored.

Known limitation: sub-agents with direct filesystem access (Claude Code with
``bypassPermissions``) can still write manifests without this flow; live reload then picks
such a change up without a confirmation. See docs/assistant_v1/domains/capabilities.md.
"""

import asyncio
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
import yaml

from assistant.core.capabilities.atomic_file import atomic_write_bytes
from assistant.core.capabilities.edit_summary import (
    SensitiveValueTooLongError,
    SummaryTooLargeError,
    capability_change_summary,
)
from assistant.core.capabilities.interfaces import CapabilityCatalogInterface
from assistant.core.capabilities.loader import ManifestParseError, parse_capability_manifest
from assistant.core.capabilities.manifest_structure import (
    ManifestStructureError,
    check_manifest_structure,
    describe_yaml_error,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilitySnapshot

logger = structlog.get_logger(__name__)
# The git work-tree warning is emitted at most once per process.
_git_warning_emitted = False

HISTORY_DIR_NAME = ".capability_history"
DEFAULT_HISTORY_RETENTION = 50

STATUS_APPLIED = "applied"
STATUS_REJECTED_INVALID = "rejected_invalid"
STATUS_NOT_EDITABLE = "not_editable"
STATUS_STALE = "stale"
STATUS_BACKUP_FAILED = "backup_failed"
STATUS_WRITE_FAILED = "write_failed"
STATUS_RELOAD_REJECTED = "reload_rejected"

OP_EDIT = "edit"
OP_ROLLBACK = "rollback"

# Capability ids become history directory names: no separators, no hidden names.
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_HISTORY_ID_RE = re.compile(r"\d{8}T\d{12}Z__[0-9a-f]{8}")
_REASON_MAX_CHARS = 1000


class CapabilityEditError(Exception):
    """A proposal or apply step was refused; ``status`` is one of the STATUS_* values."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class ManifestView:
    """Raw manifest text plus the precondition hash an edit must be based on."""

    capability_id: str
    file: str
    self_edit: bool
    raw_yaml: str
    base_sha256: str
    revision: int


@dataclass(frozen=True)
class HistoryEntry:
    """One backup in a capability's version history (the content *before* an edit)."""

    history_id: str
    created_at: str
    sha256: str
    reason: str
    confirmation_id: str
    op: str

    def to_dict(self) -> dict[str, str]:
        """Plain dict for tool results."""
        return asdict(self)


@dataclass(frozen=True)
class EditProposal:
    """A validated change ready to become a ``capability_edit`` confirmation."""

    payload: dict[str, Any]
    summary: str
    dedupe_key: str


@dataclass(frozen=True)
class EditOutcome:
    """Real result of applying an approved edit (never ``ok`` unless the write happened)."""

    ok: bool
    status: str
    detail: str
    revision_before: int | None = None
    revision_after: int | None = None
    history_id: str | None = None  # the backup written for this apply (kept on success)


@dataclass(frozen=True)
class _Target:
    capability_id: str
    path: Path  # the real file (symlinks resolved): what is read and written
    snapshot: CapabilitySnapshot


@dataclass(frozen=True)
class _Current:
    raw: bytes
    text: str
    sha256: str
    definition: CapabilityDefinition


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CapabilityEditService:
    """Read, propose, apply, list history and roll back capability manifest edits."""

    def __init__(
        self,
        catalog: CapabilityCatalogInterface,
        *,
        history_dir: Path | None = None,
        retention: int = DEFAULT_HISTORY_RETENTION,
        clock: Callable[[], datetime] = _utc_now,
        known_tool_ids: Collection[str] | None = None,
    ) -> None:
        if retention < 1:
            raise ValueError("retention must be at least 1")
        self._catalog = catalog
        self._known_tool_ids = frozenset(known_tool_ids) if known_tool_ids is not None else None
        self._config_dir = catalog.current().capabilities_dir.parent
        self._history_dir = history_dir or self._config_dir / HISTORY_DIR_NAME
        self._retention = retention
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._warn_if_in_git_work_tree()

    def _warn_if_in_git_work_tree(self) -> None:
        """Backups copy manifests verbatim, including inline MCP ``env`` secrets.

        Warns (at most once per process) when the history dir is inside a git work tree
        and git does not ignore it; when git cannot tell (not installed, not a real
        repository), it warns to be safe.
        """
        global _git_warning_emitted
        if _git_warning_emitted:
            return
        git_root = find_git_work_tree(self._history_dir)
        if git_root is None or git_ignores(git_root, self._history_dir):
            return
        _git_warning_emitted = True
        logger.warning(
            "capability_edit.history_in_git_work_tree",
            history_dir=str(self._history_dir),
            git_work_tree=str(git_root),
            hint=(
                "capability history backups may contain secrets from MCP env in the "
                "manifests, and git does not (or cannot confirm that it does) ignore "
                "them; add .capability_history/ to .gitignore and never commit it"
            ),
        )

    @property
    def history_dir(self) -> Path:
        """Root of the version history (``<config_dir>/.capability_history``)."""
        return self._history_dir

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def editable_ids(self) -> list[str]:
        """Ids of the current snapshot's manifests that :meth:`read` would accept.

        A parsed ``self_edit`` of True is not enough: YAML 1.1 ``yes`` / ``on`` also parse
        as True, and a file may hide a duplicate key. Each candidate runs the same checks
        as :meth:`read` (plain ``self_edit: true``, structure, path), so the agent is never
        offered a capability that then answers ``not_editable``.
        """
        snapshot = self._catalog.current()
        ids: list[str] = []
        for capability_id, definition in sorted(snapshot.definitions.items()):
            if definition.self_edit is not True:
                continue
            try:
                self._read_current(self._resolve(capability_id))
            except CapabilityEditError:
                continue
            ids.append(capability_id)
        return ids

    def read(self, capability_id: str) -> ManifestView:
        """Raw text and precondition hash of an editable manifest."""
        target = self._resolve(capability_id)
        current = self._read_current(target)
        return ManifestView(
            capability_id=capability_id,
            file=target.path.name,
            self_edit=current.definition.self_edit,
            raw_yaml=current.text,
            base_sha256=current.sha256,
            revision=target.snapshot.revision,
        )

    def history(self, capability_id: str, limit: int = 10) -> list[HistoryEntry]:
        """Newest-first backups of an editable manifest."""
        self._resolve(capability_id)
        entries: list[HistoryEntry] = []
        for history_id in self._history_ids(capability_id)[: max(1, limit)]:
            meta = self._read_sidecar(capability_id, history_id)
            if meta is None:
                continue
            entries.append(
                HistoryEntry(
                    history_id=history_id,
                    created_at=str(meta.get("created_at", "")),
                    sha256=str(meta.get("sha256", "")),
                    reason=str(meta.get("reason", "")),
                    confirmation_id=str(meta.get("confirmation_id", "")),
                    op=str(meta.get("op", "")),
                )
            )
        return entries

    # ------------------------------------------------------------------
    # Proposals (no writes)
    # ------------------------------------------------------------------

    def propose_edit(
        self,
        capability_id: str,
        old_string: str,
        new_string: str,
        reason: str,
        base_sha256: str,
    ) -> EditProposal:
        """Validate an exact-match edit against the file on disk; nothing is written."""
        reason = _check_reason(reason)
        target = self._resolve(capability_id)
        current = self._read_current(target)
        _check_base(target, current, base_sha256)
        new_text = _replace_exact(current.text, old_string, new_string)
        after = self._validate_result(target, current, new_text)
        new_sha = _sha256(new_text.encode("utf-8"))
        payload = {
            "op": OP_EDIT,
            "capability_id": capability_id,
            "file": str(target.path),
            "base_sha256": current.sha256,
            "new_sha256": new_sha,
            "old_string": old_string,
            "new_string": new_string,
            "reason": reason,
            "base_revision": target.snapshot.revision,
        }
        summary = _summary(
            capability_id=capability_id,
            file_name=target.path.name,
            reason=reason,
            before=current.definition,
            after=after,
        )
        return EditProposal(
            payload=payload,
            summary=summary,
            dedupe_key=_dedupe_key(capability_id, current.sha256, OP_EDIT, old_string, new_string),
        )

    def propose_rollback(
        self, capability_id: str, history_id: str, reason: str, base_sha256: str
    ) -> EditProposal:
        """Validate restoring a history version; nothing is written."""
        reason = _check_reason(reason)
        target = self._resolve(capability_id)
        current = self._read_current(target)
        _check_base(target, current, base_sha256)
        restored = self._read_history_entry(capability_id, history_id)
        new_sha = _sha256(restored)
        if new_sha == current.sha256:
            raise CapabilityEditError(
                STATUS_REJECTED_INVALID,
                f"history entry {history_id} is identical to the current manifest",
            )
        new_text = _decode(restored, f"history entry {history_id}")
        after = self._validate_result(target, current, new_text)
        payload = {
            "op": OP_ROLLBACK,
            "capability_id": capability_id,
            "file": str(target.path),
            "base_sha256": current.sha256,
            "new_sha256": new_sha,
            "target_history_id": history_id,
            "reason": reason,
            "base_revision": target.snapshot.revision,
        }
        summary = _summary(
            capability_id=capability_id,
            file_name=target.path.name,
            reason=reason,
            before=current.definition,
            after=after,
            headline=f"Capability rollback to {history_id} ({new_sha[:8]})",
        )
        return EditProposal(
            payload=payload,
            summary=summary,
            dedupe_key=_dedupe_key(capability_id, current.sha256, OP_ROLLBACK, history_id),
        )

    # ------------------------------------------------------------------
    # Apply (on approval)
    # ------------------------------------------------------------------

    async def apply(
        self,
        payload: dict[str, Any],
        *,
        confirmation_id: str,
        session_id: str,
        user_id: str | None,
    ) -> EditOutcome:
        """Re-validate and apply an approved proposal; file I/O runs in a worker thread.

        Serialized per capability, so of two approvals for the same base the second one
        finds the file changed and fails as stale.
        """
        capability_id = str(payload.get("capability_id", ""))
        async with self._lock_for(capability_id):
            outcome = await asyncio.to_thread(
                self._apply_sync, payload, confirmation_id, session_id, user_id
            )
        log = logger.info if outcome.ok else logger.warning
        event = "capability_edit.failed"
        if outcome.ok:
            event = (
                "capability_edit.rolled_back"
                if payload.get("op") == OP_ROLLBACK
                else "capability_edit.applied"
            )
        log(
            event,
            confirmation_id=confirmation_id,
            session_id=session_id,
            user_id=user_id,
            capability_id=capability_id,
            op=payload.get("op"),
            status=outcome.status,
            detail=outcome.detail,
            base_sha256=payload.get("base_sha256"),
            new_sha256=payload.get("new_sha256"),
            revision_before=outcome.revision_before,
            revision_after=outcome.revision_after,
            history_id=outcome.history_id,
        )
        return outcome

    def _apply_sync(
        self,
        payload: dict[str, Any],
        confirmation_id: str,
        session_id: str,
        user_id: str | None,
    ) -> EditOutcome:
        try:
            op = payload["op"]
            capability_id = payload["capability_id"]
            base_sha = payload["base_sha256"]
            new_sha = payload["new_sha256"]
            reason = str(payload.get("reason", ""))
        except KeyError as exc:
            return EditOutcome(False, STATUS_REJECTED_INVALID, f"malformed payload: missing {exc}")
        try:
            target = self._resolve(capability_id)
            if str(target.path) != payload.get("file"):
                raise CapabilityEditError(
                    STATUS_NOT_EDITABLE,
                    f"the manifest of '{capability_id}' moved since the proposal; nothing was "
                    "written. Re-read and propose again.",
                )
            current = self._read_current(target)
            if current.sha256 != base_sha:
                return EditOutcome(
                    False,
                    STATUS_STALE,
                    f"{target.path.name} changed between proposal and approval (expected "
                    f"{base_sha[:8]}, found {current.sha256[:8]}); nothing was written. "
                    "Re-read and propose again.",
                )
            if op == OP_EDIT:
                new_text = _replace_exact(
                    current.text,
                    str(payload.get("old_string", "")),
                    str(payload.get("new_string", "")),
                )
            elif op == OP_ROLLBACK:
                history_id = str(payload.get("target_history_id", ""))
                new_text = _decode(
                    self._read_history_entry(capability_id, history_id),
                    f"history entry {history_id}",
                )
            else:
                raise CapabilityEditError(STATUS_REJECTED_INVALID, f"unknown op {op!r}")
            self._validate_result(target, current, new_text)
        except CapabilityEditError as exc:
            return EditOutcome(False, exc.status, exc.reason)
        new_bytes = new_text.encode("utf-8")
        if _sha256(new_bytes) != new_sha:
            return EditOutcome(
                False,
                STATUS_REJECTED_INVALID,
                "the result differs from the approved proposal; nothing was written.",
            )

        revision_before = target.snapshot.revision
        meta = {
            "op": op,
            "capability_id": capability_id,
            "file": target.path.name,
            "sha256": current.sha256,
            "new_sha256": new_sha,
            "reason": reason,
            "confirmation_id": confirmation_id,
            "session_id": session_id,
            "user_id": user_id,
            "base_revision": revision_before,
            "target_history_id": payload.get("target_history_id"),
        }
        try:
            history_id = self._write_backup(capability_id, current.raw, meta)
        except OSError as exc:
            return EditOutcome(
                False,
                STATUS_BACKUP_FAILED,
                f"backup failed ({exc.strerror or exc}); nothing was written.",
            )
        try:
            atomic_write_bytes(target.path, new_bytes)
        except OSError as exc:
            self._remove_entry(capability_id, history_id)
            return EditOutcome(
                False,
                STATUS_WRITE_FAILED,
                f"writing {target.path.name} failed ({exc.strerror or exc}); "
                "the manifest is unchanged.",
            )

        snapshot = self._catalog.reload(reason=f"capability_edit:{confirmation_id}")
        state = snapshot.file_for(capability_id)
        if state is None or state.sha256 != new_sha:
            return self._restore(target, current, history_id, snapshot, confirmation_id)
        self._prune(capability_id)
        return EditOutcome(
            True,
            STATUS_APPLIED,
            f"{'rolled back' if op == OP_ROLLBACK else 'applied'} to {target.path.name}: "
            f"revision {revision_before} → {snapshot.revision}; previous version saved as "
            f"history_id {history_id}",
            revision_before=revision_before,
            revision_after=snapshot.revision,
            history_id=history_id,
        )

    def _restore(
        self,
        target: _Target,
        current: _Current,
        history_id: str,
        snapshot: CapabilitySnapshot,
        confirmation_id: str,
    ) -> EditOutcome:
        errors = "; ".join(i.message for i in snapshot.blocking_errors) or "new content not served"
        try:
            atomic_write_bytes(target.path, current.raw)
        except OSError as exc:
            logger.error(
                "capability_edit.restore_failed",
                capability_id=target.capability_id,
                confirmation_id=confirmation_id,
                history_id=history_id,
                error=str(exc),
            )
            return EditOutcome(
                False,
                STATUS_RELOAD_REJECTED,
                f"the catalog rejected the new manifest ({errors}) and restoring the previous "
                f"content failed; restore history_id {history_id} manually.",
                revision_before=target.snapshot.revision,
                revision_after=snapshot.revision,
                history_id=history_id,
            )
        restored = self._catalog.reload(reason=f"capability_edit_restore:{confirmation_id}")
        self._remove_entry(target.capability_id, history_id)
        logger.warning(
            "capability_edit.restored",
            capability_id=target.capability_id,
            confirmation_id=confirmation_id,
            errors=errors,
        )
        return EditOutcome(
            False,
            STATUS_RELOAD_REJECTED,
            f"the catalog rejected the new manifest ({errors}); the previous content was "
            "restored and nothing changed.",
            revision_before=target.snapshot.revision,
            revision_after=restored.revision,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve(self, capability_id: str) -> _Target:
        snapshot = self._catalog.current()
        if not isinstance(capability_id, str) or not _SAFE_ID_RE.fullmatch(capability_id):
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE, f"invalid capability_id {capability_id!r}"
            )
        state = snapshot.file_for(capability_id)
        if state is None:
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE,
                f"unknown capability '{capability_id}' (creating capabilities is not supported)",
            )
        try:
            real = state.path.resolve(strict=True)
            root = self._config_dir.resolve(strict=True)
        except OSError as exc:
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE, f"manifest of '{capability_id}' is missing on disk"
            ) from exc
        history_root = self._history_dir.resolve()
        if not real.is_relative_to(root) or real.is_relative_to(history_root):
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE,
                f"manifest of '{capability_id}' resolves outside the active config dir",
            )
        if not real.is_file():
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE, f"manifest of '{capability_id}' is not a regular file"
            )
        return _Target(capability_id=capability_id, path=real, snapshot=snapshot)

    def _read_current(self, target: _Target) -> _Current:
        try:
            raw = target.path.read_bytes()
        except OSError as exc:
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE, f"cannot read {target.path.name}: {exc.strerror or exc}"
            ) from exc
        try:
            definition = _parse(target.path.name, raw)
        except ManifestParseError as exc:
            cause = exc.__cause__
            detail = (
                f"YAML parse error: {describe_yaml_error(cause)}"
                if isinstance(cause, yaml.YAMLError)
                else "; ".join(exc.messages)
            )
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE, f"{target.path.name} does not parse on disk: {detail}"
            ) from exc
        if definition.capability_id != target.capability_id:
            raise CapabilityEditError(
                STATUS_STALE,
                f"{target.path.name} now defines '{definition.capability_id}'; re-read and retry",
            )
        if not definition.self_edit:
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE,
                f"capability '{target.capability_id}' is not opted in to agent edits "
                "(self_edit: true must be set by a human)",
            )
        text = _decode(raw, target.path.name, status=STATUS_NOT_EDITABLE)
        try:
            # Only the plain boolean ``true`` opts in (not YAML 1.1 ``yes``/``on``), and a
            # file that already hides a second opt-in is not editable at all.
            check_manifest_structure(text)
        except ManifestStructureError as exc:
            raise CapabilityEditError(
                STATUS_NOT_EDITABLE,
                f"{target.path.name} on disk is not editable by the agent: {exc}",
            ) from exc
        return _Current(raw=raw, text=text, sha256=_sha256(raw), definition=definition)

    def _validate_result(
        self, target: _Target, current: _Current, new_text: str
    ) -> CapabilityDefinition:
        """Every check an edit, a rollback and an approval run on the resulting text."""
        name = target.path.name
        # 1. Node tree: protected keys exist exactly once, no duplicate/merge keys, no aliases.
        try:
            check_manifest_structure(new_text)
        except ManifestStructureError as exc:
            raise CapabilityEditError(
                STATUS_REJECTED_INVALID, f"the resulting manifest is refused: {exc}"
            ) from exc
        # 2. Schema.
        try:
            after = _parse(name, new_text.encode("utf-8"))
        except ManifestParseError as exc:
            raise CapabilityEditError(
                STATUS_REJECTED_INVALID,
                f"the resulting manifest is invalid: {'; '.join(exc.messages)}",
            ) from exc
        # 3. Parsed values of the protected keys are unchanged.
        check_protected_fields(target.capability_id, current.definition, after)
        # 4. Added tool references exist in the tools registry.
        self._check_tool_references(current.definition, after)
        # 5. Cross-file checks a catalog reload would run.
        definitions = dict(target.snapshot.definitions)
        definitions[target.capability_id] = after
        issues = [i.message for i in self._catalog.validate_candidate(definitions) if i.blocking]
        if issues:
            raise CapabilityEditError(
                STATUS_REJECTED_INVALID,
                "the resulting manifest fails cross-file validation: " + "; ".join(issues),
            )
        return after

    def _check_tool_references(
        self, before: CapabilityDefinition, after: CapabilityDefinition
    ) -> None:
        if self._known_tool_ids is None:
            return
        old = set(before.tool_overrides) | {b.tool_id for b in before.tools}
        unknown = sorted(
            tool_id
            for tool_id in (*after.tool_overrides, *(b.tool_id for b in after.tools))
            if tool_id not in old and tool_id not in self._known_tool_ids
        )
        if unknown:
            raise CapabilityEditError(
                STATUS_REJECTED_INVALID,
                "unknown tool ids (not in tools.yaml) in tools / tool_overrides: "
                + ", ".join(repr(t) for t in dict.fromkeys(unknown)),
            )

    def _entry_dir(self, capability_id: str) -> Path:
        return self._history_dir / capability_id

    def _history_ids(self, capability_id: str) -> list[str]:
        directory = self._entry_dir(capability_id)
        if not directory.is_dir():
            return []
        ids = {p.stem for p in directory.glob("*.yaml") if _HISTORY_ID_RE.fullmatch(p.stem)}
        return sorted(ids, reverse=True)

    def _read_sidecar(self, capability_id: str, history_id: str) -> dict[str, Any] | None:
        path = self._entry_dir(capability_id) / f"{history_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "capability_edit.history_sidecar_unreadable",
                capability_id=capability_id,
                history_id=history_id,
                error=str(exc),
            )
            return None
        return data if isinstance(data, dict) else None

    def _read_history_entry(self, capability_id: str, history_id: str) -> bytes:
        if not isinstance(history_id, str) or not _HISTORY_ID_RE.fullmatch(history_id):
            raise CapabilityEditError(STATUS_REJECTED_INVALID, f"invalid history_id {history_id!r}")
        path = self._entry_dir(capability_id) / f"{history_id}.yaml"
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise CapabilityEditError(
                STATUS_REJECTED_INVALID,
                f"unknown history_id {history_id} for '{capability_id}' (see capability_history)",
            ) from exc
        meta = self._read_sidecar(capability_id, history_id)
        if meta is None or meta.get("sha256") != _sha256(content):
            raise CapabilityEditError(
                STATUS_REJECTED_INVALID, f"history entry {history_id} is corrupted (hash mismatch)"
            )
        return content

    def _write_backup(self, capability_id: str, raw: bytes, meta: dict[str, Any]) -> str:
        directory = self._entry_dir(capability_id)
        directory.mkdir(parents=True, exist_ok=True)
        now = self._clock().astimezone(UTC)
        sha8 = _sha256(raw)[:8]
        while True:
            history_id = f"{now:%Y%m%dT%H%M%S%f}Z__{sha8}"
            if not (directory / f"{history_id}.yaml").exists():
                break
            now += timedelta(microseconds=1)
        sidecar = {"history_id": history_id, "created_at": now.isoformat(), **meta}
        atomic_write_bytes(directory / f"{history_id}.yaml", raw)
        try:
            atomic_write_bytes(
                directory / f"{history_id}.json",
                json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except OSError:
            with suppress(OSError):
                (directory / f"{history_id}.yaml").unlink()
            raise
        return history_id

    def _remove_entry(self, capability_id: str, history_id: str) -> None:
        for suffix in (".yaml", ".json"):
            with suppress(OSError):
                (self._entry_dir(capability_id) / f"{history_id}{suffix}").unlink()

    def _prune(self, capability_id: str) -> None:
        for history_id in self._history_ids(capability_id)[self._retention :]:
            self._remove_entry(capability_id, history_id)
            logger.info(
                "capability_edit.history_pruned", capability_id=capability_id, history_id=history_id
            )

    def _lock_for(self, capability_id: str) -> asyncio.Lock:
        lock = self._locks.get(capability_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[capability_id] = lock
        return lock


def _parse(file_name: str, raw: bytes) -> CapabilityDefinition:
    """``parse_capability_manifest``; an unexpected exception (a parser / schema bug on
    agent-controlled text) becomes a ManifestParseError instead of escaping the tool."""
    try:
        return parse_capability_manifest(file_name, raw)
    except ManifestParseError:
        raise
    except Exception as exc:
        # The message is not logged or returned: it could quote manifest content.
        logger.warning(
            "capability_edit.unexpected_parse_error",
            file=file_name,
            error_type=type(exc).__name__,
        )
        raise ManifestParseError(
            [f"[{file_name}] unexpected validation error ({type(exc).__name__})"]
        ) from exc


def _summary(**kwargs: Any) -> str:
    """Approver summary; a change whose ⚠ lines do not all fit is refused, never cut."""
    try:
        return capability_change_summary(**kwargs)
    except SummaryTooLargeError as exc:
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID,
            f"the change has {exc.sensitive_changes} security-sensitive field changes, too "
            "many to show the user in one confirmation; split it into smaller edits",
        ) from exc
    except SensitiveValueTooLongError as exc:
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID,
            f"the security-sensitive change at {exc.path} needs {exc.chars} chars to show the "
            f"user in full (limit {exc.limit}); it cannot be shortened without hiding part of "
            "it, so propose a smaller edit (change less of this value at once) or ask the "
            "user to make the change by hand",
        ) from exc
    except Exception as exc:
        logger.warning("capability_edit.summary_failed", error_type=type(exc).__name__)
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID,
            f"the change summary for the user could not be built ({type(exc).__name__}); "
            "nothing was proposed",
        ) from exc


def git_ignores(git_root: Path, path: Path) -> bool:
    """True only when ``git check-ignore`` confirms that files under ``path`` are ignored.

    A file inside the directory is probed, so a ``dir/`` pattern matches even before the
    directory exists. Any failure (git missing, not a repository, timeout) is False.
    """
    probe = path.resolve() / "probe.yaml"
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(git_root), "check-ignore", "-q", "--", str(probe)],  # noqa: S607
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find_git_work_tree(path: Path) -> Path | None:
    """Closest ancestor of ``path`` (or itself) that contains a ``.git`` entry."""
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _check_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise CapabilityEditError(STATUS_REJECTED_INVALID, "reason must not be empty")
    if len(reason) > _REASON_MAX_CHARS:
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID, f"reason is longer than {_REASON_MAX_CHARS} chars"
        )
    return reason


def _check_base(target: _Target, current: _Current, base_sha256: str) -> None:
    if current.sha256 != (base_sha256 or "").strip().lower():
        raise CapabilityEditError(
            STATUS_STALE,
            f"{target.path.name} changed since you read it (current sha256 "
            f"{current.sha256[:8]}); call capability_read again",
        )


def _decode(raw: bytes, what: str, status: str = STATUS_REJECTED_INVALID) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapabilityEditError(status, f"{what} is not UTF-8 text") from exc


def check_protected_fields(
    capability_id: str, before: CapabilityDefinition, after: CapabilityDefinition
) -> None:
    """Parsed ``capability_id`` and ``self_edit`` must be unchanged (and ``self_edit`` true)."""
    if after.capability_id != capability_id or after.capability_id != before.capability_id:
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID,
            f"capability_id must stay '{capability_id}' (renaming or creating "
            "capabilities is not supported)",
        )
    if after.self_edit is not True or after.self_edit != before.self_edit:
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID,
            "self_edit must stay true; only a human may change it",
        )


def _replace_exact(text: str, old_string: str, new_string: str) -> str:
    """Exact, unique replacement (the protected keys are checked on the result)."""
    if not old_string:
        raise CapabilityEditError(STATUS_REJECTED_INVALID, "old_string must not be empty")
    if old_string == new_string:
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID, "old_string and new_string are identical"
        )
    count = text.count(old_string)
    if count == 0:
        raise CapabilityEditError(STATUS_REJECTED_INVALID, "old_string not found in the manifest")
    if count > 1:
        raise CapabilityEditError(
            STATUS_REJECTED_INVALID,
            f"old_string is not unique ({count} occurrences); include more surrounding context",
        )
    start = text.index(old_string)
    return text[:start] + new_string + text[start + len(old_string) :]


def _dedupe_key(capability_id: str, base_sha256: str, *parts: str) -> str:
    material = json.dumps([capability_id, base_sha256, *parts], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
