"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Pydantic AI tool registration and tool helper logic.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic_ai import Agent, RunContext

from assistant.extensions.first_party.memory import MemoryProposalToolCall
from assistant.extensions.first_party.memory import (
    memory_propose_update as validate_memory_proposal,
)

MAX_MEMORY_WRITES_PER_TURN = 3

logger = structlog.get_logger(__name__)


@dataclass
class TurnDeps:
    """Dependencies injected into agent tools for turn execution."""

    writes_approved: list[None]  # mutable: append when we approve a write
    seen_intent_ids: set[str]  # mutable: deduplicate intent_id per turn
    memory_search_handler: Callable[[str, int, list[str] | None], dict[str, Any]] | None = None


def normalize_candidate_for_upsert(candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize loosely structured model candidate into memory schema-friendly payload."""
    payload = dict(candidate or {})
    body = payload.get("body_markdown")
    if isinstance(body, str) and body.strip():
        payload["body_markdown"] = body.strip()
        return payload

    reserved = {"tags", "entities", "priority", "confidence", "body_markdown"}
    details: list[tuple[str, Any]] = []
    for key, value in payload.items():
        if key in reserved:
            continue
        if value in (None, "", [], {}):
            continue
        details.append((key, value))
    if details:
        payload["body_markdown"] = "\n".join(f"- {k}: {v}" for k, v in details)
    else:
        payload["body_markdown"] = "[missing details]"

    tags = payload.get("tags")
    if not isinstance(tags, list):
        payload["tags"] = []
    entities = payload.get("entities")
    if not isinstance(entities, list):
        payload["entities"] = []
    name = payload.get("name")
    if isinstance(name, str) and name.strip() and name not in payload["entities"]:
        payload["entities"].append(name.strip())
    if not payload["tags"]:
        payload["tags"] = ["user_profile"]
    return payload


def register_agent_tools(agent: Agent[TurnDeps, str]) -> None:
    """Register runtime tools on the provided agent instance."""

    @agent.tool
    def memory_search(
        ctx: RunContext[TurnDeps],
        query: str,
        limit: int = 3,
        memory_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Retrieve compact memory context on demand."""
        handler = ctx.deps.memory_search_handler
        bounded_limit = max(1, min(limit, 5))
        if handler is None:
            logger.info(
                "provider.tool_call.memory_search",
                status="unavailable",
                query=query,
                limit=bounded_limit,
                memory_types=memory_types or [],
            )
            return {
                "status": "unavailable",
                "reason": "memory retrieval unavailable",
                "matches": [],
            }
        try:
            result = handler(query, bounded_limit, memory_types)
            logger.info(
                "provider.tool_call.memory_search",
                status=result.get("status", "ok"),
                query=query,
                limit=bounded_limit,
                memory_types=memory_types or [],
                match_count=len(result.get("matches", [])) if isinstance(result, dict) else 0,
            )
            return result
        except Exception as exc:
            logger.warning(
                "provider.tool_call.memory_search",
                status="failed",
                query=query,
                limit=bounded_limit,
                memory_types=memory_types or [],
                error=str(exc),
            )
            return {"status": "failed", "reason": str(exc), "matches": []}

    @agent.tool
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
        deps = ctx.deps
        normalized_candidate = candidate or {}
        if action == "upsert":
            normalized_candidate = normalize_candidate_for_upsert(candidate)
        # Runtime policy controls confirmation; do not allow model bypass.
        effective_requires_confirmation = True
        proposal_dict = {
            "intent_id": intent_id,
            "action": action,
            "memory_type": memory_type,
            "memory_id": memory_id,
            "candidate": normalized_candidate,
            "reason": reason,
            "source": source,
            "requires_user_confirmation": effective_requires_confirmation,
        }
        try:
            proposal = MemoryProposalToolCall(**proposal_dict)  # type: ignore[arg-type]
        except Exception as exc:
            logger.info(
                "provider.tool_call.memory_propose_update",
                status="rejected_invalid",
                intent_id=intent_id,
                action=action,
                memory_type=memory_type,
                error=str(exc),
            )
            return {
                "status": "rejected_invalid",
                "reason": str(exc),
                "requires_user_confirmation": effective_requires_confirmation,
            }

        if proposal.intent_id in deps.seen_intent_ids:
            logger.info(
                "provider.tool_call.memory_propose_update",
                status="rejected_duplicate_intent",
                intent_id=proposal.intent_id,
                action=proposal.action,
                memory_type=proposal.memory_type,
            )
            return {
                "status": "rejected_duplicate_intent",
                "reason": "duplicate intent_id in turn payload",
                "requires_user_confirmation": effective_requires_confirmation,
            }
        deps.seen_intent_ids.add(proposal.intent_id)

        if effective_requires_confirmation:
            logger.info(
                "provider.tool_call.memory_propose_update",
                status="pending_confirmation",
                intent_id=proposal.intent_id,
                action=proposal.action,
                memory_type=proposal.memory_type,
            )
            return {
                "status": "pending_confirmation",
                "reason": "requires_user_confirmation=true",
                "requires_user_confirmation": True,
            }

        if len(deps.writes_approved) >= MAX_MEMORY_WRITES_PER_TURN:
            logger.info(
                "provider.tool_call.memory_propose_update",
                status="rejected_write_limit",
                intent_id=proposal.intent_id,
                action=proposal.action,
                memory_type=proposal.memory_type,
            )
            return {
                "status": "rejected_write_limit",
                "reason": f"exceeds max writes per turn ({MAX_MEMORY_WRITES_PER_TURN})",
                "requires_user_confirmation": False,
            }

        try:
            validate_memory_proposal(proposal.model_dump())
        except Exception as exc:
            logger.info(
                "provider.tool_call.memory_propose_update",
                status="rejected_invalid",
                intent_id=proposal.intent_id,
                action=proposal.action,
                memory_type=proposal.memory_type,
                error=str(exc),
            )
            return {
                "status": "rejected_invalid",
                "reason": str(exc),
                "requires_user_confirmation": effective_requires_confirmation,
            }

        deps.writes_approved.append(None)
        logger.info(
            "provider.tool_call.memory_propose_update",
            status="approved_pending_apply",
            intent_id=proposal.intent_id,
            action=proposal.action,
            memory_type=proposal.memory_type,
        )
        return {
            "status": "approved_pending_apply",
            "reason": "",
            "requires_user_confirmation": False,
        }
