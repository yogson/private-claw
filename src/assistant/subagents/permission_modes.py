"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code permission-mode resolution shared by the coordinator and the
Claude Code delegation backends.

The configured ``delegation_default_permission_mode`` tool param (built-in
default ``"auto"``) is both the default and the ceiling.  A per-request
``backend_params["permission_mode"]`` may only pick a mode that is as
restrictive or more (see ``PERMISSION_MODE_RANK``); a less restrictive one is
refused with a structured warning and clamped to the configured mode.  Unknown
values never escalate either: they fall back to the configured mode with a
structured warning.
"""

from collections.abc import Mapping
from typing import Any, Literal, get_args

import structlog

from assistant.core.config.schemas import SubagentPermissionMode, normalize_permission_mode_alias

logger = structlog.get_logger(__name__)

DEFAULT_PERMISSION_MODE: SubagentPermissionMode = "auto"
SUPPORTED_PERMISSION_MODES: tuple[SubagentPermissionMode, ...] = get_args(SubagentPermissionMode)
# The CLI shows "default" as "Manual" and newer CLIs accept "manual" as an
# alias, but older ones (e.g. 2.1.81, bundled with claude-agent-sdk 0.1.50) do
# not, so "manual" is mapped to "default" before anything reaches the CLI.
_MODES_BY_NAME: dict[str, SubagentPermissionMode] = {m: m for m in SUPPORTED_PERMISSION_MODES}

# Restrictiveness order, most restrictive first; a higher rank means the
# sub-agent may do more without anyone approving it.  In a delegated run nobody
# can answer a permission prompt (``claude -p`` denies it, the streaming
# backend's can_use_tool denies everything outside bypassPermissions), so:
# - plan: read-only analysis; no file edits or commands that change anything.
# - dontAsk: the Manual rules, but every call that would prompt is denied up
#   front - including AskUserQuestion, so even the user relay is unavailable.
# - default (Manual): allow/deny rules decide; prompts are denied, except
#   AskUserQuestion, which the streaming backend relays to the user.
# - acceptEdits: as default, plus file edits in the working directories are
#   approved without a prompt.
# - auto: a classifier approves most actions, Bash included, and blocks only
#   destructive, irreversible or outward-facing ones.
# - bypassPermissions: no checks at all (only protected-path safeguards).
PERMISSION_MODE_RANK: dict[SubagentPermissionMode, int] = {
    "plan": 0,
    "dontAsk": 1,
    "default": 2,
    "acceptEdits": 3,
    "auto": 4,
    "bypassPermissions": 5,
}

_CONFIG_HINT = (
    "set delegation_default_permission_mode (tools.yaml, delegate_subagent_task) "
    "to another mode, e.g. 'acceptEdits' (file edits are approved, but Bash commands "
    "outside the Claude Code allow list are denied, and the claude_code_streaming "
    "backend loads no allow list)"
)

StreamEventKind = Literal["system", "model_output", "other"]


def parse_permission_mode(raw: object) -> SubagentPermissionMode | None:
    """Return the canonical mode for ``raw``, or None if it is not a known mode."""
    value = normalize_permission_mode_alias(raw)
    if not isinstance(value, str):
        return None
    return _MODES_BY_NAME.get(value)


def resolve_permission_mode(
    raw: object,
    default: SubagentPermissionMode = DEFAULT_PERMISSION_MODE,
    *,
    source: str,
    **log_context: Any,
) -> SubagentPermissionMode:
    """Resolve a requested permission mode, falling back to ``default``.

    Empty/None means "not requested" and silently yields ``default``; any other
    unknown value logs ``subagent.permission_mode.unknown`` and yields ``default``.
    ``log_context`` (task_id, trace_id, session_id, ...) is added to the warning.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    mode = parse_permission_mode(raw)
    if mode is not None:
        return mode
    logger.warning(
        "subagent.permission_mode.unknown",
        source=source,
        requested=str(raw),
        fallback=default,
        supported=list(SUPPORTED_PERMISSION_MODES),
        **log_context,
    )
    return default


def clamp_permission_mode(
    requested: SubagentPermissionMode,
    ceiling: SubagentPermissionMode,
    **log_context: Any,
) -> SubagentPermissionMode:
    """Return ``requested`` unless it is less restrictive than ``ceiling``.

    A less restrictive request is refused: it logs
    ``subagent.permission_mode.clamped`` (with ``log_context``) and yields
    ``ceiling``, so no caller can raise a run above the configured mode.
    """
    if PERMISSION_MODE_RANK[requested] <= PERMISSION_MODE_RANK[ceiling]:
        return requested
    logger.warning(
        "subagent.permission_mode.clamped",
        requested=requested,
        ceiling=ceiling,
        reason="per-request permission_mode is less restrictive than the configured mode",
        **log_context,
    )
    return ceiling


def permission_mode_mismatch_error(
    requested: SubagentPermissionMode,
    reported: object,
    model: object = None,
) -> str | None:
    """Return an actionable error when the CLI started in another mode than requested.

    Claude Code does not fail when ``--permission-mode auto`` is unavailable
    (unsupported model, disabled by settings/organization): it silently starts
    in Manual ("default") mode, where a headless run denies everything outside
    the allow list.  ``system`` events (``init``, ``status``) report the
    effective mode.
    """
    if not isinstance(reported, str) or not reported:
        return None  # older CLI without the field: nothing to compare against
    if parse_permission_mode(reported) == requested:
        return None
    message = (
        f"Claude Code started the sub-agent in '{reported}' permission mode instead of "
        f"the requested '{requested}'; the run was aborted before the model's first response."
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
    come before the model's first output, so every report until the first
    model output (assistant/user/result message) is checked.  Other events
    (``rate_limit_event``, partial ``stream_event`` ...) do not end the check.

    This is about message order only: the CLI does not wait for us, so it may
    already be waiting on the first API round-trip when we kill it.  It cannot
    have acted on a model response yet, so no tool call can have been made
    based on one.  Later changes are only logged: the backends never
    auto-approve prompts outside bypassPermissions, so a mid-run downgrade
    cannot escalate anything.
    """

    def __init__(
        self,
        requested: SubagentPermissionMode,
        task_id: str,
        model: str | None = None,
    ) -> None:
        self._requested = requested
        self._task_id = task_id
        self._started = False
        # Fallback until a system event names the model: `claude -p` sends
        # the downgrading `status` event before `init`, and it has no model.
        self._model: object = model or None
        self._last_reported: object = None

    def observe(self, kind: StreamEventKind, data: Mapping[str, Any]) -> str | None:
        """Feed one stream message; return an error if the run must be aborted."""
        if kind == "model_output":
            self._started = True
            return None
        if kind != "system":
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
            model=self._model,
        )
        return None if self._started else error


def explain_cli_rejection(requested: SubagentPermissionMode, stderr: str) -> str | None:
    """Return an actionable error when the CLI rejected ``--permission-mode``."""
    if "--permission-mode" not in stderr:
        return None
    return (
        f"Claude Code CLI rejected --permission-mode {requested}: {stderr.strip()}"
        f" Upgrade Claude Code to a version that supports --permission-mode {requested},"
        f" or {_CONFIG_HINT}."
    )
