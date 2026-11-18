"""
Component ID: CMP_CORE_CAPABILITIES

Claude Code side effects of the active capabilities: sub-agent MCP servers and the
``~/.claude/settings.json`` permissions merge, plus a catalog subscriber that re-applies
them when a new snapshot revision changes the relevant manifest fields.
"""

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import structlog

from assistant.core.capabilities.claude_settings_file import (
    SettingsFileError,
    read_settings_file,
    write_settings_file,
)
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilitySnapshot

logger = structlog.get_logger(__name__)


def collect_claude_code_mcp_servers(
    definitions: Mapping[str, CapabilityDefinition],
    enabled_capabilities: list[str],
) -> dict[str, dict[str, Any]]:
    """Merge ``claude_code_settings.mcp_servers`` from the active capabilities.

    Servers are merged in ``enabled_capabilities`` order; duplicate ids are overwritten
    by later capabilities.  The result is handed to the delegation backends, which pass
    it to Claude Code via ``--mcp-config`` — the CLI ignores ``mcpServers`` in
    settings.json, so that file cannot be used to deliver MCP servers to sub-agents.
    """
    merged: dict[str, dict[str, Any]] = {}
    for cap_id in enabled_capabilities:
        definition = definitions.get(cap_id)
        if definition is None or definition.claude_code_settings is None:
            continue
        for name, spec in definition.claude_code_settings.mcp_servers.items():
            merged[name] = copy.deepcopy(spec)
    return merged


def apply_claude_code_settings(
    definitions: Mapping[str, CapabilityDefinition],
    enabled_capabilities: list[str],
    settings_path: Path | None = None,
) -> bool:
    """Merge claude_code_settings from active capabilities into ~/.claude/settings.json.

    ``enabled_capabilities`` must be the caller's already-expanded, already-denied-filtered
    list of capability IDs.  This function does not expand nested capabilities or filter
    denied ones — that is the caller's responsibility (consistent with all other use sites).

    Permissions are union-merged: all allow/deny entries from all active capabilities
    are combined (duplicates removed, order preserved).

    MCP servers from capabilities are merged in enabled_capabilities order; duplicate
    server ids are overwritten by later capabilities. Written as top-level ``mcpServers``.
    If no capability defines ``mcp_servers``, the existing ``mcpServers`` key is left unchanged.
    Note that Claude Code ignores ``mcpServers`` in settings.json — the key is written for
    reference only; sub-agents get their servers via ``collect_claude_code_mcp_servers``,
    which the delegation backends pass to the CLI as ``--mcp-config``.

    Only ``permissions.allow``, ``permissions.deny`` and ``mcpServers`` are owned here;
    every other key is preserved. The write is atomic and skipped when nothing changes. An
    existing file that is unreadable or not a JSON object is never overwritten, and a
    failed write (read-only directory, full disk, ...) is never raised: in both cases a
    warning is logged and ``False`` is returned. Returns ``True`` when the file holds the
    merged settings (or there was nothing to merge).
    """
    if settings_path is None:
        settings_path = Path.home() / ".claude" / "settings.json"

    merged_allow: list[str] = []
    merged_deny: list[str] = []
    seen_allow: set[str] = set()
    seen_deny: set[str] = set()
    merged_mcp = collect_claude_code_mcp_servers(definitions, enabled_capabilities)

    for cap_id in enabled_capabilities:
        definition = definitions.get(cap_id)
        if definition is None or definition.claude_code_settings is None:
            continue
        perms = definition.claude_code_settings.permissions
        for entry in perms.allow:
            if entry not in seen_allow:
                merged_allow.append(entry)
                seen_allow.add(entry)
        for entry in perms.deny:
            if entry not in seen_deny:
                merged_deny.append(entry)
                seen_deny.add(entry)

    if not merged_allow and not merged_deny and not merged_mcp:
        return True

    try:
        existing = read_settings_file(settings_path)
    except SettingsFileError as exc:
        return _skip(settings_path, str(exc))
    merged = copy.deepcopy(existing)

    if merged_allow or merged_deny:
        permissions = merged.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            return _skip(settings_path, "'permissions' is not a JSON object")
        # allow/deny are owned wholesale (entries added by hand are replaced); see #56.
        permissions["allow"] = merged_allow
        permissions["deny"] = merged_deny

    if merged_mcp:
        merged["mcpServers"] = merged_mcp

    if merged != existing:
        try:
            write_settings_file(settings_path, merged)
        except OSError as exc:  # read-only ~/.claude or target, full disk, ...
            return _skip(settings_path, f"cannot write settings: {exc}")
    return True


def _skip(settings_path: Path, reason: str) -> bool:
    logger.warning(
        "capabilities.claude_code_settings.skipped",
        path=str(settings_path),
        error=reason,
        hint="settings.json left untouched; fix the file or its directory to apply them",
    )
    return False


def _settings_fingerprint(
    definitions: Mapping[str, CapabilityDefinition], active: Sequence[str]
) -> str:
    payload = [
        (cap_id, definition.claude_code_settings.model_dump(mode="json"))
        for cap_id in active
        if (definition := definitions.get(cap_id)) is not None
        and definition.claude_code_settings is not None
    ]
    return json.dumps(payload, sort_keys=True)


class ClaudeCodeSettingsSync:
    """Re-apply :func:`apply_claude_code_settings` when a revision changes the inputs.

    Registered as a catalog subscriber. It tracks the ``claude_code_settings`` of the
    active capabilities (resolved by the caller-supplied ``resolve_active`` callback) and
    writes ``settings.json`` only when they differ from what was applied last, so revisions
    that touch only prompts or tools never rewrite the file.
    """

    def __init__(
        self,
        initial: CapabilitySnapshot,
        resolve_active: Callable[[CapabilitySnapshot], list[str]],
        settings_path: Path | None = None,
    ) -> None:
        self._resolve_active = resolve_active
        self._settings_path = settings_path
        self._applied = _settings_fingerprint(initial.definitions, resolve_active(initial))

    def __call__(self, snapshot: CapabilitySnapshot) -> None:
        """Apply the settings of ``snapshot`` if they changed since the last apply."""
        active = self._resolve_active(snapshot)
        fingerprint = _settings_fingerprint(snapshot.definitions, active)
        if fingerprint == self._applied:
            return
        if not apply_claude_code_settings(snapshot.definitions, active, self._settings_path):
            return  # skipped (unusable settings.json): retried with the next revision
        self._applied = fingerprint
        logger.info(
            "capabilities.claude_code_settings.reapplied",
            revision=snapshot.revision,
            active_capabilities=active,
        )


class McpServersTarget(Protocol):
    """Anything that hands MCP servers to sub-agent runs (the delegation backends)."""

    def set_mcp_servers(self, mcp_servers: dict[str, dict[str, Any]]) -> None:
        """Replace the servers used by sub-agent runs started from now on."""


class SubagentMcpServersSync:
    """Recompute sub-agent MCP servers for every new revision and push them to targets.

    Registered as a catalog subscriber; ``resolve_active`` picks the active capabilities
    (the default policy) of a snapshot.
    """

    def __init__(
        self,
        resolve_active: Callable[[CapabilitySnapshot], list[str]],
        targets: Sequence[McpServersTarget],
    ) -> None:
        self._resolve_active = resolve_active
        self._targets = list(targets)

    def servers_for(self, snapshot: CapabilitySnapshot) -> dict[str, dict[str, Any]]:
        """Merged ``claude_code_settings.mcp_servers`` of the snapshot's active set."""
        return collect_claude_code_mcp_servers(snapshot.definitions, self._resolve_active(snapshot))

    def __call__(self, snapshot: CapabilitySnapshot) -> None:
        """Push the servers of ``snapshot`` to every target."""
        servers = self.servers_for(snapshot)
        for target in self._targets:
            target.set_mcp_servers(servers)
