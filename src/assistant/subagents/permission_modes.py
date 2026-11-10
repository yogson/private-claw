"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code permission-mode resolution shared by the coordinator and the
Claude Code delegation backends.

The mode a sub-agent runs in comes from ``backend_params["permission_mode"]``
(explicit per-request override), else the configured
``delegation_default_permission_mode`` tool param, else ``"auto"``.  Unknown
values never escalate to ``bypassPermissions``: they fall back to the default
with a structured warning.
"""

from collections.abc import Mapping
from typing import Any, get_args

import structlog

from assistant.core.config.schemas import SubagentPermissionMode

logger = structlog.get_logger(__name__)

DEFAULT_PERMISSION_MODE: SubagentPermissionMode = "auto"
SUPPORTED_PERMISSION_MODES: tuple[SubagentPermissionMode, ...] = get_args(SubagentPermissionMode)
# The CLI shows "default" as "Manual" and accepts "manual" as an alias, but
# older CLIs (e.g. the copy bundled with claude-agent-sdk) only know "default".
_ALIASES: dict[str, SubagentPermissionMode] = {"manual": "default"}
_MODES_BY_NAME: dict[str, SubagentPermissionMode] = {m: m for m in SUPPORTED_PERMISSION_MODES}

_CONFIG_HINT = (
    "set delegation_default_permission_mode (tools.yaml, delegate_subagent_task) "
    "to another mode, e.g. 'acceptEdits' or 'dontAsk'"
)


def parse_permission_mode(raw: object) -> SubagentPermissionMode | None:
    """Return the canonical mode for ``raw``, or None if it is not a known mode."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return _MODES_BY_NAME.get(value) or _ALIASES.get(value.lower())


def resolve_permission_mode(
    raw: object,
    default: SubagentPermissionMode = DEFAULT_PERMISSION_MODE,
    *,
    source: str,
    task_id: str | None = None,
) -> SubagentPermissionMode:
    """Resolve a requested permission mode, falling back to ``default``.

    Empty/None means "not requested" and silently yields ``default``; any other
    unknown value logs ``subagent.permission_mode.unknown`` and yields ``default``.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    mode = parse_permission_mode(raw)
    if mode is not None:
        return mode
    logger.warning(
        "subagent.permission_mode.unknown",
        source=source,
        task_id=task_id,
        requested=str(raw),
        fallback=default,
        supported=list(SUPPORTED_PERMISSION_MODES),
    )
    return default


def permission_mode_mismatch_error(
    requested: SubagentPermissionMode,
    reported: object,
    model: object = None,
) -> str | None:
    """Return an actionable error when the CLI started in another mode than requested.

    Claude Code does not fail when ``--permission-mode auto`` is unavailable
    (unsupported model, disabled by settings/organization): it silently starts
    in Manual ("default") mode, where a headless run denies everything outside
    the allow list.  The ``system/init`` event reports the effective mode.
    """
    if not isinstance(reported, str) or not reported:
        return None  # older CLI without the field: nothing to compare against
    if parse_permission_mode(reported) == requested:
        return None
    message = (
        f"Claude Code started the sub-agent in '{reported}' permission mode instead of "
        f"the requested '{requested}'; the run was aborted before any tool call."
    )
    if requested == "auto":
        message += (
            f" Auto mode is unavailable for this session (model '{model or 'unknown'}' may "
            "not support it - it needs Opus 4.6+, Sonnet 4.6+, Haiku 5.5 or a Fable model - "
            "or it is disabled by permissions.disableAutoMode or for the account)."
            f" Use a supported model, or {_CONFIG_HINT}."
        )
    else:
        message += f" Check Claude Code settings/managed policy, or {_CONFIG_HINT}."
    return message


class PermissionModeMonitor:
    """Watches the permission mode the CLI reports while a run starts up.

    The effective mode arrives on ``system`` events (``init``, ``status``) in
    an order that depends on the input mode: with ``claude -p`` a ``status``
    event saying "default" precedes ``init``; over the SDK's stream-json input
    ``init`` still says "auto" and a following ``status`` downgrades it.  Both
    come before the first assistant message, so every report up to the first
    non-system message is checked - no tool can have run by then.  Later
    changes are only logged: the backends never auto-approve prompts outside
    bypassPermissions, so a mid-run downgrade cannot escalate anything.
    """

    def __init__(self, requested: SubagentPermissionMode, task_id: str) -> None:
        self._requested = requested
        self._task_id = task_id
        self._started = False
        self._model: object = None
        self._last_reported: object = None

    def observe(self, is_system: bool, data: Mapping[str, Any]) -> str | None:
        """Feed one stream message; return an error if the run must be aborted."""
        if not is_system:
            self._started = True
            return None
        self._model = data.get("model") or self._model
        reported = data.get("permissionMode")
        if reported is None or reported == self._last_reported:
            return None
        self._last_reported = reported
        error = permission_mode_mismatch_error(self._requested, reported, self._model)
        if error is None:
            return None
        logger.warning(
            "subagent.permission_mode.changed"
            if self._started
            else "subagent.permission_mode.mismatch",
            task_id=self._task_id,
            requested=self._requested,
            reported=reported,
        )
        return None if self._started else error


def explain_cli_rejection(requested: SubagentPermissionMode, stderr: str) -> str | None:
    """Return an actionable error when the CLI rejected ``--permission-mode``."""
    if "--permission-mode" not in stderr:
        return None
    return (
        f"Claude Code CLI rejected --permission-mode {requested}: {stderr.strip()}"
        " Upgrade Claude Code (auto mode needs v2.1.228+), or " + _CONFIG_HINT + "."
    )
