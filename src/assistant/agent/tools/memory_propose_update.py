"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Memory propose update tool for Pydantic AI agent.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import MAX_MEMORY_WRITES_PER_TURN, TurnDeps
from assistant.extensions.first_party.memory import (
    MemoryProposalToolCall,
    canonicalize_memory_args,
    normalize_candidate_for_upsert,
)
from assistant.extensions.first_party.memory import (
    memory_propose_update as validate_memory_proposal,
)

logger = structlog.get_logger(__name__)


def memory_propose_update(
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
    """Propose a memory update. Runtime applies policy and confirmation gates."""
    logger.info(
        "provider.tool_call.memory_propose_update",
        phase="entry",
        intent_id=intent_id,
        action=action,
        memory_type=memory_type,
        reason=reason,
        source=source,
        requires_user_confirmation=requires_user_confirmation,
        memory_id=memory_id,
        has_candidate=candidate is not None,
    )
    deps = ctx.deps
    proposal_dict = {
        "intent_id": intent_id,
        "action": action,
        "memory_type": memory_type,
        "memory_id": memory_id,
        "candidate": candidate,
        "reason": reason,
        "source": source,
        "requires_user_confirmation": True,
    }
    canonicalize_memory_args(proposal_dict)
    effective_requires_confirmation = True
    if proposal_dict["action"] == "upsert":
        raw_cand = proposal_dict.get("candidate")
        cand = raw_cand if isinstance(raw_cand, dict) else {}
        proposal_dict["candidate"] = normalize_candidate_for_upsert(cand)
    try:
        proposal = MemoryProposalToolCall(**proposal_dict)  # type: ignore[arg-type]
    except Exception as exc:
        result = {
            "status": "rejected_invalid",
            "reason": str(exc),
            "requires_user_confirmation": effective_requires_confirmation,
        }
        logger.info(
            "provider.tool_result.memory_propose_update",
            status=result["status"],
            reason=result["reason"],
        )
        return result

    if proposal.intent_id in deps.seen_intent_ids:
        result = {
            "status": "rejected_duplicate_intent",
            "reason": "duplicate intent_id in turn payload",
            "requires_user_confirmation": effective_requires_confirmation,
        }
        logger.info(
            "provider.tool_result.memory_propose_update",
            status=result["status"],
            reason=result["reason"],
        )
        return result
    deps.seen_intent_ids.add(proposal.intent_id)

    if effective_requires_confirmation:
        result = {
            "status": "pending_confirmation",
            "reason": "requires_user_confirmation=true",
            "requires_user_confirmation": True,
        }
        logger.info(
            "provider.tool_result.memory_propose_update",
            status=result["status"],
            reason=result["reason"],
        )
        return result

    if len(deps.writes_approved) >= MAX_MEMORY_WRITES_PER_TURN:
        result = {
            "status": "rejected_write_limit",
            "reason": f"exceeds max writes per turn ({MAX_MEMORY_WRITES_PER_TURN})",
            "requires_user_confirmation": False,
        }
        logger.info(
            "provider.tool_result.memory_propose_update",
            status=result["status"],
            reason=result["reason"],
        )
        return result

    try:
        validate_memory_proposal(proposal.model_dump())
    except Exception as exc:
        result = {
            "status": "rejected_invalid",
            "reason": str(exc),
            "requires_user_confirmation": effective_requires_confirmation,
        }
        logger.info(
            "provider.tool_result.memory_propose_update",
            status=result["status"],
            reason=result["reason"],
        )
        return result

    deps.writes_approved.append(None)
    result = {
        "status": "approved_pending_apply",
        "reason": "",
        "requires_user_confirmation": False,
    }
    logger.info(
        "provider.tool_result.memory_propose_update",
        status=result["status"],
    )
    return result
