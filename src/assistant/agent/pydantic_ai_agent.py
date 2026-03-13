"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Pydantic AI Agent runtime for turn execution with typed memory_propose_update tool.
Replaces manual provider tool protocol handling with Agent-managed tool loop.
"""

import json
from datetime import datetime
from typing import Any, cast

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from assistant.agent.tools import TurnDeps, register_agent_tools
from assistant.core.orchestrator.memory import MemoryIntentPlan
from assistant.core.prompts import load_prompt
from assistant.extensions.first_party.memory import (
    MemoryProposalToolCall,
    normalize_candidate_for_upsert,
)
from assistant.extensions.first_party.memory import (
    memory_propose_update as validate_memory_proposal,
)
from assistant.store.models import (
    AssistantToolCallPayload,
    SessionRecord,
    SessionRecordType,
    ToolResultPayload,
)

__all__ = [
    "PydanticAITurnAdapter",
    "TurnDeps",
    "_new_messages_to_plans",
    "_new_messages_to_session_records",
    "_llm_messages_to_history",
    "_normalize_candidate_for_upsert",
]

MEMORY_TOOL_NAME = "memory_propose_update"
MEMORY_AGENT_PROMPT_NAME = "memory_agent_system"


def _normalize_candidate_for_upsert(candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Backward-compatible wrapper for existing tests/importers."""
    return normalize_candidate_for_upsert(candidate)


def _create_memory_agent(model_id: str, system_prompt: str) -> Agent[TurnDeps, str]:
    """Create Agent with memory_propose_update tool. Output type is str (final assistant text)."""
    agent = Agent(
        model_id,
        deps_type=TurnDeps,
        system_prompt=system_prompt,
        retries=0,
    )
    register_agent_tools(agent)
    return agent


def _message_to_prompt_content(msg: dict[str, Any]) -> str | list[dict[str, Any]]:
    """Extract prompt content from a message dict."""
    if msg.get("content_blocks"):
        return cast(list[dict[str, Any]], msg["content_blocks"])
    return str(msg.get("content", "") or "")


def _llm_messages_to_history(messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """Convert LLM messages (incl. tool_use/tool_result content_blocks) to pydantic_ai format.

    Preserves tool calls and tool results for correct replay/restore of conversation context.
    """
    history: list[ModelMessage] = []
    for msg in messages:
        role = msg.get("role", "")
        content = _message_to_prompt_content(msg)
        if role == "user":
            if isinstance(content, str):
                if content.strip():
                    history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
            elif isinstance(content, list):
                tool_return_parts: list[ToolReturnPart] = []
                text_content = ""
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "tool_result":
                            tool_return_parts.append(
                                ToolReturnPart(
                                    tool_call_id=part.get("tool_use_id", ""),
                                    tool_name=part.get("tool_name", "unknown"),
                                    content=part.get("content", ""),
                                )
                            )
                        elif part.get("type") == "text":
                            text_content = part.get("text", "") or ""
                if tool_return_parts:
                    history.append(ModelRequest(parts=tool_return_parts))
                elif text_content.strip():
                    history.append(ModelRequest(parts=[UserPromptPart(content=text_content)]))
        elif role == "assistant":
            text_parts: list[str] = []
            tool_call_parts: list[ToolCallPart] = []
            if isinstance(content, str) and content.strip():
                text_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            t = part.get("text", "")
                            if t:
                                text_parts.append(t)
                        elif part.get("type") == "tool_use":
                            args = part.get("input")
                            if not isinstance(args, dict):
                                args = {}
                            tool_call_parts.append(
                                ToolCallPart(
                                    tool_call_id=part.get("id", ""),
                                    tool_name=part.get("name", ""),
                                    args=args,
                                )
                            )
            parts: list[TextPart | ToolCallPart] = []
            if text_parts:
                parts.append(TextPart(content="\n\n".join(text_parts)))
            parts.extend(tool_call_parts)
            if parts:
                history.append(ModelResponse(parts=parts))
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


def _new_messages_to_session_records(
    new_messages: list[ModelMessage],
    *,
    session_id: str,
    turn_id: str,
    timestamp: datetime,
    assistant_msg_id: str,
    model_id: str | None = None,
    skip_memory_tool_results: bool = True,
) -> list[SessionRecord]:
    """Convert pydantic_ai new_messages to session records for full replay/restore.

    Emits ASSISTANT_MESSAGE, ASSISTANT_TOOL_CALL, and TOOL_RESULT records in
    chronological order. When skip_memory_tool_results is True, memory_propose_update
    tool results are omitted (caller persists them separately with applied outcomes).
    """
    records: list[SessionRecord] = []
    assistant_idx = 0

    for msg in new_messages:
        if isinstance(msg, ModelResponse):
            text_parts = [p for p in msg.parts if isinstance(p, TextPart)]
            tool_call_parts = [p for p in msg.parts if isinstance(p, ToolCallPart)]

            if text_parts:
                content = " ".join(p.content for p in text_parts if p.content).strip()
                msg_id = (
                    assistant_msg_id
                    if assistant_idx == 0
                    else f"{assistant_msg_id}-{assistant_idx}"
                )
                payload: dict[str, Any] = {
                    "message_id": msg_id,
                    "content": content or "",
                }
                if model_id:
                    payload["model_id"] = model_id
                records.append(
                    SessionRecord(
                        session_id=session_id,
                        sequence=0,
                        event_id=msg_id,
                        turn_id=turn_id,
                        timestamp=timestamp,
                        record_type=SessionRecordType.ASSISTANT_MESSAGE,
                        payload=payload,
                    )
                )
                assistant_idx += 1

            for part in tool_call_parts:
                args = part.args if isinstance(part.args, dict) else {}
                args_json = json.dumps(args, separators=(",", ":"))
                call_payload = AssistantToolCallPayload(
                    message_id=f"msg-{part.tool_call_id}",
                    tool_call_id=part.tool_call_id,
                    tool_name=part.tool_name,
                    arguments_json=args_json,
                )
                records.append(
                    SessionRecord(
                        session_id=session_id,
                        sequence=0,
                        event_id=f"assistant-tool-call-{part.tool_call_id}",
                        turn_id=turn_id,
                        timestamp=timestamp,
                        record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
                        payload=call_payload.model_dump(),
                    )
                )

        elif isinstance(msg, ModelRequest):
            for req_part in msg.parts:
                if not isinstance(req_part, ToolReturnPart):
                    continue
                if skip_memory_tool_results and req_part.tool_name == MEMORY_TOOL_NAME:
                    continue
                result = _parse_tool_result_content(req_part.content)
                result_payload = ToolResultPayload(
                    message_id=f"msg-result-{req_part.tool_call_id}",
                    tool_call_id=req_part.tool_call_id,
                    tool_name=req_part.tool_name,
                    result=result,
                    error=None,
                )
                records.append(
                    SessionRecord(
                        session_id=session_id,
                        sequence=0,
                        event_id=f"tool-result-{req_part.tool_call_id}",
                        turn_id=turn_id,
                        timestamp=timestamp,
                        record_type=SessionRecordType.TOOL_RESULT,
                        payload=result_payload.model_dump(),
                    )
                )

    return records


class PydanticAITurnAdapter:
    """Runs one orchestrator turn via Pydantic AI Agent."""

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 1024,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._system_prompt = load_prompt(MEMORY_AGENT_PROMPT_NAME)
        self._agent = _create_memory_agent(model_id, self._system_prompt)

    @property
    def system_prompt(self) -> str:
        """System prompt used for each turn."""
        return self._system_prompt

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
