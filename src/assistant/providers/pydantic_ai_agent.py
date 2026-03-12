"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Pydantic AI Agent runtime for turn execution with typed memory_propose_update tool.
Replaces manual provider tool protocol handling with Agent-managed tool loop.
"""

import json
from dataclasses import dataclass
from typing import Any, cast

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from assistant.core.orchestrator.memory import MemoryIntentPlan
from assistant.extensions.first_party.memory import (
    MemoryProposalToolCall,
)
from assistant.extensions.first_party.memory import (
    memory_propose_update as validate_memory_proposal,
)

MEMORY_TOOL_NAME = "memory_propose_update"
MAX_MEMORY_WRITES_PER_TURN = 3


@dataclass
class TurnDeps:
    """Dependencies injected into agent tools for turn execution."""

    writes_approved: list[None]  # mutable: append when we approve a write
    seen_intent_ids: set[str]  # mutable: deduplicate intent_id per turn


def _normalize_candidate_for_upsert(candidate: dict[str, Any] | None) -> dict[str, Any]:
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


def _create_memory_agent(model_id: str) -> Agent[TurnDeps, str]:
    """Create Agent with memory_propose_update tool. Output type is str (final assistant text)."""

    agent = Agent(
        model_id,
        deps_type=TurnDeps,
        system_prompt=(
            "You are a helpful assistant. When the user asks to remember something, "
            "use the memory_propose_update tool to propose the update. "
            "Do not write memory directly; runtime applies policy and confirmation gates. "
            "Do not claim memory is saved until the user confirms."
        ),
        retries=0,
    )

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
            normalized_candidate = _normalize_candidate_for_upsert(candidate)
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
            return {
                "status": "rejected_invalid",
                "reason": str(exc),
                "requires_user_confirmation": effective_requires_confirmation,
            }

        if proposal.intent_id in deps.seen_intent_ids:
            return {
                "status": "rejected_duplicate_intent",
                "reason": "duplicate intent_id in turn payload",
                "requires_user_confirmation": effective_requires_confirmation,
            }
        deps.seen_intent_ids.add(proposal.intent_id)

        if effective_requires_confirmation:
            return {
                "status": "pending_confirmation",
                "reason": "requires_user_confirmation=true",
                "requires_user_confirmation": True,
            }

        if len(deps.writes_approved) >= MAX_MEMORY_WRITES_PER_TURN:
            return {
                "status": "rejected_write_limit",
                "reason": f"exceeds max writes per turn ({MAX_MEMORY_WRITES_PER_TURN})",
                "requires_user_confirmation": False,
            }

        try:
            validate_memory_proposal(proposal.model_dump())
        except Exception as exc:
            return {
                "status": "rejected_invalid",
                "reason": str(exc),
                "requires_user_confirmation": effective_requires_confirmation,
            }

        deps.writes_approved.append(None)
        return {
            "status": "approved_pending_apply",
            "reason": "",
            "requires_user_confirmation": False,
        }

    return agent


def _message_to_prompt_content(msg: dict[str, Any]) -> str | list[dict[str, Any]]:
    """Extract prompt content from a message dict."""
    if msg.get("content_blocks"):
        return cast(list[dict[str, Any]], msg["content_blocks"])
    return str(msg.get("content", "") or "")


def _llm_messages_to_history(messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """Convert simple role/content message list to pydantic_ai ModelMessage format."""
    history: list[ModelMessage] = []
    for msg in messages:
        role = msg.get("role", "")
        content = _message_to_prompt_content(msg)
        if not content:
            continue
        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))  # type: ignore[arg-type]
        elif role == "assistant":
            text = content if isinstance(content, str) else ""
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        break
            history.append(ModelResponse(parts=[TextPart(content=text)]))
    return history


def _parse_tool_result_content(content: str | Any) -> dict[str, Any]:
    """Parse tool return content to dict. Handles JSON string or dict."""
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            result: dict[str, Any] = json.loads(content)
            return result
        except json.JSONDecodeError:
            return {"status": "failed", "reason": f"invalid json: {content[:100]}"}
    return {"status": "failed", "reason": "unknown content type"}


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
        effective_args["candidate"] = _normalize_candidate_for_upsert(
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


class PydanticAITurnAdapter:
    """Runs one orchestrator turn via Pydantic AI Agent."""

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 1024,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._agent = _create_memory_agent(model_id)

    async def run_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        deps: TurnDeps,
        trace_id: str,
    ) -> tuple[str, list[ModelMessage], dict[str, int] | None]:
        """
        Execute one turn. messages includes history + current user message (last).
        Returns (response_text, new_messages, usage).
        """
        if not messages:
            return "", [], None
        user_prompt = _message_to_prompt_content(messages[-1])
        history_msgs = messages[:-1] if len(messages) > 1 else []
        history = _llm_messages_to_history(history_msgs)
        model_settings = {"max_tokens": self._max_tokens}
        result = await self._agent.run(  # type: ignore[call-overload]
            user_prompt,
            message_history=history,
            deps=deps,
            model=self._model_id,
            model_settings=model_settings,
        )
        response_text = result.output if isinstance(result.output, str) else str(result.output)
        new_msgs = result.new_messages()
        usage = None
        if result.usage:
            usage = {
                "input_tokens": getattr(result.usage, "request_tokens", 0) or 0,
                "output_tokens": getattr(result.usage, "response_tokens", 0) or 0,
            }
        return response_text, list(new_msgs), usage
