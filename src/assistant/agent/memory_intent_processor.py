"""
Memory intent processing for the pydantic_ai agent.

Extracts and normalizes memory tool calls from new_messages into
MemoryIntentPlan objects ready for orchestrator persistence.
"""

import json
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

from assistant.agent.constants import MEMORY_TOOL_NAME
from assistant.agent.message_converters import _parse_tool_result_content
from assistant.core.orchestrator.memory import MemoryIntentPlan
from assistant.extensions.first_party.memory import (
    MemoryProposalToolCall,
    normalize_candidate_for_upsert,
)
from assistant.extensions.first_party.memory import (
    memory_propose_update as validate_memory_proposal,
)


def _canonicalize_intent_id(intent_id: Any, tool_call_id: str) -> str:
    """Create a per-tool-call intent id to avoid cross-turn collisions."""
    base = str(intent_id).strip() if isinstance(intent_id, str) and intent_id.strip() else "intent"
    return f"{base}:{tool_call_id}"


def _collect_memory_tool_events(
    new_messages: list[ModelMessage],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Collect memory tool calls/results keyed by tool_call_id."""
    tool_calls: dict[str, dict[str, Any]] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    for msg in new_messages:
        if isinstance(msg, ModelResponse):
            for resp_part in msg.parts:
                if isinstance(resp_part, ToolCallPart) and resp_part.tool_name == MEMORY_TOOL_NAME:
                    args = resp_part.args if isinstance(resp_part.args, dict) else {}
                    tool_calls[resp_part.tool_call_id] = args
        elif isinstance(msg, ModelRequest):
            for req_part in msg.parts:
                if isinstance(req_part, ToolReturnPart) and req_part.tool_name == MEMORY_TOOL_NAME:
                    tool_results[req_part.tool_call_id] = _parse_tool_result_content(
                        req_part.content
                    )
    return tool_calls, tool_results


def _build_effective_memory_args(tool_call_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize and normalize tool-call arguments before persistence/apply."""
    effective_args = dict(args)
    effective_args["intent_id"] = _canonicalize_intent_id(
        effective_args.get("intent_id"), tool_call_id
    )
    # Runtime policy controls confirmation regardless of model-provided value.
    effective_args["requires_user_confirmation"] = True
    if effective_args.get("action") == "upsert":
        candidate = effective_args.get("candidate")
        effective_args["candidate"] = normalize_candidate_for_upsert(
            candidate if isinstance(candidate, dict) else {}
        )
    return effective_args


def _build_memory_intent_plan(
    tool_call_id: str,
    effective_args: dict[str, Any],
    result: dict[str, Any],
) -> MemoryIntentPlan:
    """Build one MemoryIntentPlan from normalized args and tool result."""
    intent_json = json.dumps(effective_args, separators=(",", ":"))
    status = result.get("status", "failed")
    reason = result.get("reason", "")
    requires_confirmation = bool(result.get("requires_user_confirmation", False))

    intent = None
    if status == "approved_pending_apply":
        try:
            proposal = MemoryProposalToolCall(**effective_args)
            intent = validate_memory_proposal(proposal.model_dump())
        except Exception as exc:
            status = "rejected_invalid"
            reason = str(exc)
            requires_confirmation = bool(effective_args.get("requires_user_confirmation", False))
    return MemoryIntentPlan(
        tool_call_id=tool_call_id,
        intent_json=intent_json,
        precheck_status=status,
        requires_confirmation=requires_confirmation,
        intent=intent,
        reason=reason,
    )


def _new_messages_to_plans(new_messages: list[ModelMessage]) -> list[MemoryIntentPlan]:
    """Extract memory intent plans from pydantic_ai new_messages.

    Pairs ToolCallPart (from ModelResponse) with ToolReturnPart (from ModelRequest)
    by tool_call_id. Only memory_propose_update tool calls are processed.
    """
    tool_calls, tool_results = _collect_memory_tool_events(new_messages)
    plans: list[MemoryIntentPlan] = []
    missing_result = {"status": "failed", "reason": "missing tool result"}
    for tool_call_id, args in tool_calls.items():
        effective_args = _build_effective_memory_args(tool_call_id, args)
        result = tool_results.get(tool_call_id, missing_result)
        plans.append(_build_memory_intent_plan(tool_call_id, effective_args, result))
    return plans
