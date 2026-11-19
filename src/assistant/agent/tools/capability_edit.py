"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Capability manifest self-editing tools (thin wrappers around CapabilityEditService).

Never write a manifest themselves: a valid edit or rollback becomes a pending
``capability_edit`` user confirmation (core.confirmation). The user approves or rejects it
with buttons; the real outcome (applied / failed with reason / stale) is written into
session history as a [[CONFIRMATION_RESOLVED]] record.

File I/O and validation never run on the event loop: the async propose tools call the
service through ``asyncio.to_thread``; the sync tools (read, history) are run in a worker
thread by pydantic-ai.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.core.capabilities.edit_service import (
    CapabilityEditError,
    CapabilityEditService,
    EditProposal,
)
from assistant.core.confirmation import build_capability_edit_draft
from assistant.core.confirmation.models import ConfirmationDraft, ConfirmationRequestResult

logger = structlog.get_logger(__name__)

type _Requester = Callable[[ConfirmationDraft], Awaitable[ConfirmationRequestResult]]

_PENDING_NOTE = (
    "The user sees Approve/Reject buttons with a summary of this change. Nothing is written "
    "until they approve. Do not re-propose it and do not ask the user to confirm in text; "
    "the outcome will appear in history as [[CONFIRMATION_RESOLVED]]."
)
_BLOCKED_NOTE = (
    "Another confirmation is still awaiting the user's answer in this session. This "
    "proposal was NOT registered. Retry it after the pending one is resolved (its outcome "
    "will appear in history as [[CONFIRMATION_RESOLVED]])."
)
_UNAVAILABLE = {"status": "unavailable", "reason": "capability editing is not available here"}
_HISTORY_MAX_LIMIT = 50


def capability_read(ctx: RunContext[TurnDeps], capability_id: str) -> dict[str, Any]:
    """Read the raw YAML of a capability manifest that is opted in to agent edits.

    Always call this before capability_propose_edit / capability_propose_rollback: they
    need the returned base_sha256, and old_string must match raw_yaml exactly. Only
    manifests a human marked with ``self_edit: true`` can be read or edited.

    Args:
        capability_id: Id of the capability (e.g. "assistant").
    """
    service = ctx.deps.capability_edit_service
    if service is None:
        return _UNAVAILABLE
    logger.info("provider.tool_call.capability_read", capability_id=capability_id)
    try:
        view = service.read(capability_id)
    except CapabilityEditError as exc:
        return _error(service, exc)
    except Exception as exc:
        return _unexpected("capability_read", exc)
    return {
        "status": "ok",
        "capability_id": view.capability_id,
        "file": view.file,
        "self_edit": view.self_edit,
        "raw_yaml": view.raw_yaml,
        "base_sha256": view.base_sha256,
        "revision": view.revision,
    }


async def capability_propose_edit(
    ctx: RunContext[TurnDeps],
    capability_id: str,
    old_string: str,
    new_string: str,
    reason: str,
    base_sha256: str,
) -> dict[str, Any]:
    """Propose an exact-text edit of a capability manifest; applied only after user approval.

    Works like a find-and-replace on the raw YAML from capability_read: old_string must
    occur exactly once (include surrounding context) and is replaced by new_string. Keep
    edits minimal. Any field may change except capability_id and self_edit. The result is
    validated before the user sees it. Never re-propose after a rejection without new
    input from the user.

    Args:
        capability_id: Id of the capability to edit.
        old_string: Exact text to replace (unique in the file, whitespace included).
        new_string: Replacement text. Must differ from old_string.
        reason: Short explanation for the user of why this change is needed.
        base_sha256: base_sha256 from the capability_read this edit is based on.
    """
    service = ctx.deps.capability_edit_service
    if service is None:
        return _UNAVAILABLE
    logger.info(
        "provider.tool_call.capability_propose_edit",
        capability_id=capability_id,
        old_chars=len(old_string),
        new_chars=len(new_string),
    )
    try:
        proposal = await asyncio.to_thread(
            service.propose_edit, capability_id, old_string, new_string, reason, base_sha256
        )
    except CapabilityEditError as exc:
        return _error(service, exc)
    except Exception as exc:
        return _unexpected("capability_propose_edit", exc)
    return await _request_confirmation(ctx, ctx.deps.confirmation_requester, proposal)


def capability_history(
    ctx: RunContext[TurnDeps], capability_id: str, limit: int = 10
) -> dict[str, Any]:
    """List the saved versions of a capability manifest, newest first.

    Each entry is the content as it was *before* an applied edit or rollback; pass its
    history_id to capability_propose_rollback to restore it.

    Args:
        capability_id: Id of the capability.
        limit: Maximum number of entries to return (1-50).
    """
    service = ctx.deps.capability_edit_service
    if service is None:
        return _UNAVAILABLE
    try:
        entries = service.history(capability_id, min(max(limit, 1), _HISTORY_MAX_LIMIT))
    except CapabilityEditError as exc:
        return _error(service, exc)
    except Exception as exc:
        return _unexpected("capability_history", exc)
    return {
        "status": "ok",
        "capability_id": capability_id,
        "entries": [entry.to_dict() for entry in entries],
    }


async def capability_propose_rollback(
    ctx: RunContext[TurnDeps],
    capability_id: str,
    history_id: str,
    reason: str,
    base_sha256: str,
) -> dict[str, Any]:
    """Propose restoring a saved version of a capability manifest; applied only after approval.

    The current content is backed up first, so a rollback can itself be rolled back.

    Args:
        capability_id: Id of the capability.
        history_id: history_id from capability_history.
        reason: Short explanation for the user of why the rollback is needed.
        base_sha256: base_sha256 from capability_read of the current manifest.
    """
    service = ctx.deps.capability_edit_service
    if service is None:
        return _UNAVAILABLE
    logger.info(
        "provider.tool_call.capability_propose_rollback",
        capability_id=capability_id,
        history_id=history_id,
    )
    try:
        proposal = await asyncio.to_thread(
            service.propose_rollback, capability_id, history_id, reason, base_sha256
        )
    except CapabilityEditError as exc:
        return _error(service, exc)
    except Exception as exc:
        return _unexpected("capability_propose_rollback", exc)
    return await _request_confirmation(ctx, ctx.deps.confirmation_requester, proposal)


async def _request_confirmation(
    ctx: RunContext[TurnDeps], requester: _Requester | None, proposal: EditProposal
) -> dict[str, Any]:
    if requester is None:
        return {"status": "unavailable", "reason": "user confirmation is not available here"}
    draft = build_capability_edit_draft(
        proposal, origin_tool_call_id=ctx.tool_call_id or "", origin_tool_name=ctx.tool_name or ""
    )
    outcome = await requester(draft)
    payload = proposal.payload
    if outcome.request is None:
        blocking = outcome.blocked_by
        return _result(
            {
                "status": "another_confirmation_pending",
                "pending_confirmation_id": blocking.confirmation_id if blocking else None,
                "note": _BLOCKED_NOTE,
            }
        )
    logger.info(
        "capability_edit.proposed",
        confirmation_id=outcome.request.confirmation_id,
        session_id=outcome.request.session_id,
        tool_call_id=ctx.tool_call_id,
        capability_id=payload.get("capability_id"),
        op=payload.get("op"),
        base_sha256=payload.get("base_sha256"),
        new_sha256=payload.get("new_sha256"),
        base_revision=payload.get("base_revision"),
        deduplicated=not outcome.created,
    )
    return _result(
        {
            "status": "pending_confirmation",
            "confirmation_id": outcome.request.confirmation_id,
            "deduplicated": not outcome.created,
            "note": _PENDING_NOTE,
        }
    )


def _error(service: CapabilityEditService, exc: CapabilityEditError) -> dict[str, Any]:
    result: dict[str, Any] = {"status": exc.status, "reason": exc.reason}
    if exc.status == "not_editable":
        result["editable_capabilities"] = service.editable_ids()
    return _result(result)


def _unexpected(tool: str, exc: Exception) -> dict[str, Any]:
    """A failure the service did not turn into a refusal; never fails the turn.

    Only the exception class is reported: its message could quote manifest content.
    """
    logger.warning(
        "capability_edit.tool_unexpected_error", tool=tool, error_type=type(exc).__name__
    )
    return _result(
        {
            "status": "rejected_invalid",
            "reason": f"the request failed unexpectedly ({type(exc).__name__}); nothing was "
            "proposed or written",
        }
    )


def _result(result: dict[str, Any]) -> dict[str, Any]:
    logger.info("provider.tool_result.capability_edit", **result)
    return result
