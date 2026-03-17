"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Memory intent planning and application helpers for orchestrator turns.
"""

import json
from dataclasses import dataclass
from typing import Any

from assistant.extensions.first_party.memory import (
    MemoryProposalToolCall,
    memory_propose_update,
)
from assistant.memory.interfaces import MemoryWriterInterface
from assistant.memory.write.models import MemoryUpdateIntent, WriteAudit

MAX_MEMORY_WRITES_PER_TURN = 3


@dataclass(slots=True)
class MemoryIntentPlan:
    tool_call_id: str
    intent_json: str
    precheck_status: str
    requires_confirmation: bool
    intent: MemoryUpdateIntent | None = None
    reason: str = ""


type MemoryOutcome = tuple[str, dict[str, Any], str | None]


def build_memory_intent_plans(
    tool_call_proposals: list[tuple[str, MemoryProposalToolCall]],
) -> list[MemoryIntentPlan]:
    """Build prechecked memory intent plans with policy/confirmation gates.

    Accepts (tool_call_id, proposal) pairs so provider tool_call_id is used end-to-end.
    """
    plans: list[MemoryIntentPlan] = []
    seen_intent_ids: set[str] = set()
    writes_approved = 0
    for tool_call_id, proposal in tool_call_proposals:
        intent_json = proposal.model_dump_json()
        if proposal.intent_id in seen_intent_ids:
            plans.append(
                MemoryIntentPlan(
                    tool_call_id=tool_call_id,
                    intent_json=intent_json,
                    precheck_status="rejected_duplicate_intent",
                    requires_confirmation=proposal.requires_user_confirmation,
                    reason="duplicate intent_id in turn payload",
                )
            )
            continue
        seen_intent_ids.add(proposal.intent_id)
        if proposal.requires_user_confirmation:
            plans.append(
                MemoryIntentPlan(
                    tool_call_id=tool_call_id,
                    intent_json=intent_json,
                    precheck_status="pending_confirmation",
                    requires_confirmation=True,
                    reason="requires_user_confirmation=true",
                )
            )
            continue
        if writes_approved >= MAX_MEMORY_WRITES_PER_TURN:
            plans.append(
                MemoryIntentPlan(
                    tool_call_id=tool_call_id,
                    intent_json=intent_json,
                    precheck_status="rejected_write_limit",
                    requires_confirmation=False,
                    reason=f"exceeds max writes per turn ({MAX_MEMORY_WRITES_PER_TURN})",
                )
            )
            continue
        try:
            intent = memory_propose_update(proposal.model_dump())
        except Exception as exc:  # defensive: proposal already validated
            plans.append(
                MemoryIntentPlan(
                    tool_call_id=tool_call_id,
                    intent_json=intent_json,
                    precheck_status="rejected_invalid",
                    requires_confirmation=proposal.requires_user_confirmation,
                    reason=str(exc),
                )
            )
            continue
        writes_approved += 1
        plans.append(
            MemoryIntentPlan(
                tool_call_id=tool_call_id,
                intent_json=intent_json,
                precheck_status="approved_pending_apply",
                requires_confirmation=False,
                intent=intent,
                reason="",
            )
        )
    return plans


def apply_approved_memory_intents(
    plans: list[MemoryIntentPlan],
    memory_writer: MemoryWriterInterface | None,
    user_id: str | None = None,
) -> list[MemoryOutcome]:
    """Apply approved memory plans and return normalized outcomes."""
    outcomes: list[MemoryOutcome] = []
    for plan in plans:
        if plan.precheck_status != "approved_pending_apply":
            outcomes.append(
                (
                    plan.tool_call_id,
                    {
                        "status": plan.precheck_status,
                        "reason": plan.reason,
                        "requires_user_confirmation": plan.requires_confirmation,
                    },
                    None,
                )
            )
            continue
        if not memory_writer or plan.intent is None:
            outcomes.append(
                (
                    plan.tool_call_id,
                    {
                        "status": "failed",
                        "reason": "memory writer unavailable",
                    },
                    "memory writer unavailable",
                )
            )
            continue
        try:
            audit: WriteAudit = memory_writer.apply_intent(plan.intent, user_id=user_id)
        except Exception as exc:
            outcomes.append(
                (
                    plan.tool_call_id,
                    {
                        "status": "failed",
                        "reason": str(exc),
                    },
                    str(exc),
                )
            )
            continue
        outcomes.append((plan.tool_call_id, json.loads(audit.model_dump_json()), None))
    return outcomes
