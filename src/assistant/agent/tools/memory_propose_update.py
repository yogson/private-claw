"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Memory propose update tool for Pydantic AI agent.

Never writes memory itself: a valid proposal becomes a pending user confirmation
(core.confirmation). The user approves or rejects it with buttons; the outcome is
written into session history as a [[CONFIRMATION_RESOLVED]] record.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.core.confirmation import build_memory_update_draft
from assistant.extensions.first_party.memory import memory_propose_update as validate_proposal

logger = structlog.get_logger(__name__)

_PENDING_NOTE = (
    "The user sees Approve/Reject buttons for this proposal. Do not re-propose it and do "
    "not ask the user to confirm in text; the outcome will appear in history as "
    "[[CONFIRMATION_RESOLVED]]."
)
_BLOCKED_NOTE = (
    "Another confirmation is still awaiting the user's answer in this session. This "
    "proposal was NOT registered. Retry it after the pending one is resolved (its outcome "
    "will appear in history as [[CONFIRMATION_RESOLVED]])."
)


async def memory_propose_update(
    ctx: RunContext[TurnDeps],
    intent_id: str,
    action: str,
    memory_type: str,
    reason: str,
    source: str,
    requires_user_confirmation: bool,
    memory_id: str | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a memory update. It is applied only after the user approves it via buttons."""
    logger.info(
        "provider.tool_call.memory_propose_update",
        intent_id=intent_id,
        action=action,
        memory_type=memory_type,
        source=source,
        memory_id=memory_id,
        has_candidate=candidate is not None,
    )
    try:
        intent = validate_proposal(
            {
                "intent_id": intent_id,
                "action": action,
                "memory_type": memory_type,
                "memory_id": memory_id,
                "candidate": candidate,
                "reason": reason,
                "source": source,
            }
        )
    except Exception as exc:
        return _result({"status": "rejected_invalid", "reason": str(exc)})

    requester = ctx.deps.confirmation_requester
    if requester is None:
        return _result(
            {"status": "unavailable", "reason": "user confirmation is not available here"}
        )
    draft = build_memory_update_draft(intent, origin_tool_call_id=ctx.tool_call_id or "")
    outcome = await requester(draft)
    if outcome.request is None:
        blocking = outcome.blocked_by
        return _result(
            {
                "status": "another_confirmation_pending",
                "pending_confirmation_id": blocking.confirmation_id if blocking else None,
                "note": _BLOCKED_NOTE,
            }
        )
    return _result(
        {
            "status": "pending_confirmation",
            "confirmation_id": outcome.request.confirmation_id,
            "deduplicated": not outcome.created,
            "note": _PENDING_NOTE,
        }
    )


def _result(result: dict[str, Any]) -> dict[str, Any]:
    logger.info("provider.tool_result.memory_propose_update", **result)
    return result
