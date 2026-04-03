"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Pydantic AI Agent runtime for turn execution with typed memory_propose_update tool.
Replaces manual provider tool protocol handling with Agent-managed tool loop.
"""

import asyncio
import contextlib
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

from assistant.agent.ask_question_extractor import _extract_pending_ask_question
from assistant.agent.constants import ASK_QUESTION_TOOL_NAME
from assistant.agent.memory_intent_processor import _new_messages_to_plans
from assistant.agent.message_converters import (
    _llm_messages_to_history,
    _message_to_user_prompt_content,
)
from assistant.agent.session_record_builder import _new_messages_to_session_records
from assistant.agent.system_prompt_builder import _compose_system_prompt
from assistant.agent.tools import TurnDeps, get_agent_tools
from assistant.core.config.schemas import RuntimeConfig
from assistant.extensions.first_party.memory import normalize_candidate_for_upsert

__all__ = [
    "PydanticAITurnAdapter",
    "TurnCancelledWithPartial",
    "TurnDeps",
    "_extract_pending_ask_question",
    "_new_messages_to_plans",
    "_new_messages_to_session_records",
    "_llm_messages_to_history",
    "_normalize_candidate_for_upsert",
]


class TurnCancelledWithPartial(asyncio.CancelledError):
    """Raised when a turn is cancelled; carries partial messages captured so far.

    Subclasses CancelledError so existing ``except asyncio.CancelledError`` handlers
    still work.  Callers that want the partial context check for this subclass first.
    """

    def __init__(self, partial_messages: list[ModelMessage]) -> None:
        super().__init__()
        self.partial_messages = partial_messages


def _inject_cancellation_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Return messages with synthetic 'user_cancelled' ToolReturnParts injected.

    Any ToolCallPart without a corresponding ToolReturnPart in the same message list
    gets a synthetic cancelled result appended as a new ModelRequest.  This ensures
    the persisted history satisfies the replay invariant (every tool call has a result).
    """
    completed_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    completed_ids.add(part.tool_call_id)

    pending: list[ToolCallPart] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for resp_part in msg.parts:
                if (
                    isinstance(resp_part, ToolCallPart)
                    and resp_part.tool_call_id not in completed_ids
                ):
                    pending.append(resp_part)

    if not pending:
        return messages

    cancelled_parts: list[ToolReturnPart] = [
        ToolReturnPart(
            tool_name=call.tool_name,
            content='{"status": "cancelled", "reason": "user_cancelled"}',
            tool_call_id=call.tool_call_id,
            outcome="denied",
        )
        for call in pending
    ]
    return list(messages) + [ModelRequest(parts=cancelled_parts)]


def _normalize_candidate_for_upsert(candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Backward-compatible wrapper for existing tests/importers."""
    return normalize_candidate_for_upsert(candidate)


def _create_agent(model_id: str, system_prompt: str, config: RuntimeConfig) -> Agent[TurnDeps, str]:
    """Create Agent with memory_propose_update tool. Output type is str (final assistant text)."""
    return Agent(
        model_id,
        deps_type=TurnDeps,
        system_prompt=system_prompt,
        retries=0,  # tool call retries: keep at 0 to prevent loops
        output_retries=1,  # output validation retries: allow 1 retry
        tools=get_agent_tools(config),
    )


class PydanticAITurnAdapter:
    """Runs one orchestrator turn via Pydantic AI Agent."""

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 1024,
        config: RuntimeConfig | None = None,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        if config is None:
            raise ValueError("PydanticAITurnAdapter requires config for capability-gated tools")
        self._system_prompt = _compose_system_prompt(config)
        self._agent = _create_agent(model_id, self._system_prompt, config)

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
        model_id: str | None = None,
    ) -> tuple[str, list[ModelMessage], dict[str, int] | None]:
        """
        Execute one turn. messages includes history + current user message (last).
        Returns (response_text, new_messages, usage).
        When model_id is provided, uses it for this turn; otherwise uses default.
        Raises TurnCancelledWithPartial (subclass of CancelledError) if cancelled,
        carrying whatever messages were captured before the cancellation.
        """
        from pydantic_ai._agent_graph import CallToolsNode

        if not messages:
            return "", [], None
        effective_model = model_id if model_id else self._model_id
        if effective_model and not effective_model.startswith("anthropic:"):
            effective_model = f"anthropic:{effective_model}"
        user_prompt = _message_to_user_prompt_content(messages[-1])
        history_msgs = messages[:-1] if len(messages) > 1 else []
        history = _llm_messages_to_history(history_msgs)
        # pydantic_ai only injects SystemPromptPart when message_history is empty.
        # On continuation turns, prepend it so the Anthropic backend always receives
        # the `system` parameter regardless of turn number.
        if history:
            history = [
                ModelRequest(parts=[SystemPromptPart(content=self._system_prompt)]),
                *history,
            ]
        model_settings: dict[str, Any] = {
            "max_tokens": self._max_tokens,
            "anthropic_cache_instructions": True,
            "anthropic_cache_tool_definitions": True,
        }
        async with self._agent.iter(  # type: ignore[call-overload]
            user_prompt,
            message_history=history,
            deps=deps,
            model=effective_model or self._model_id,
            model_settings=model_settings,
        ) as agent_run:
            streamed_texts: list[str] = []
            _buffered_text: str | None = None
            try:
                async for node in agent_run:
                    if isinstance(node, CallToolsNode):
                        # Flush the previously buffered text now that the tools from the
                        # prior iteration have completed.  This ensures agent messages appear
                        # *after* the actions they describe rather than before them.
                        if _buffered_text is not None and deps.streaming_text_notifier is not None:
                            with contextlib.suppress(Exception):
                                await deps.streaming_text_notifier(_buffered_text)
                            streamed_texts.append(_buffered_text)
                            _buffered_text = None

                        # Buffer any text from this response; it will be flushed once the
                        # current tool calls complete (next iteration or after the loop).
                        if deps.streaming_text_notifier is not None:
                            text_parts = [
                                p
                                for p in node.model_response.parts
                                if isinstance(p, TextPart) and p.content
                            ]
                            if text_parts:
                                _buffered_text = "\n\n".join(
                                    p.content for p in text_parts
                                ).strip()

                        if deps.tool_call_notifier is not None:
                            for part in node.model_response.parts:
                                if isinstance(part, ToolCallPart):
                                    with contextlib.suppress(Exception):
                                        await deps.tool_call_notifier(
                                            part.tool_name, part.args_as_json_str()
                                        )
            except asyncio.CancelledError:
                # Capture whatever messages were processed before cancellation so the
                # caller can persist them and keep session history intact.
                partial: list[ModelMessage] = []
                with contextlib.suppress(Exception):
                    partial = list(agent_run.new_messages())
                raise TurnCancelledWithPartial(_inject_cancellation_results(partial)) from None

            # Flush any text buffered from the last CallToolsNode: those tools have
            # now completed (loop exited normally).
            if _buffered_text is not None and deps.streaming_text_notifier is not None:
                with contextlib.suppress(Exception):
                    await deps.streaming_text_notifier(_buffered_text)
                streamed_texts.append(_buffered_text)

        result = agent_run.result
        if result is None:
            return "", [], None
        response_text = result.output
        new_msgs = result.new_messages()

        # When ask_question completed successfully, pydantic_ai still makes one final
        # LLM call after the tool returns.  That call's output (result.output) typically
        # echoes content already visible to the user, producing a duplicate.  Discard it.
        ask_question_asked = any(
            isinstance(part, ToolReturnPart)
            and part.tool_name == ASK_QUESTION_TOOL_NAME
            and '"question_asked"' in (part.content if isinstance(part.content, str) else "")
            for msg in new_msgs
            if isinstance(msg, ModelRequest)
            for part in msg.parts
        )

        # When streaming_text_notifier is set, intermediate texts were already sent to
        # the user in real time during the agent loop — do not re-include them in
        # response_text.  Without the notifier (e.g. tests), fall back to accumulation
        # so nothing is lost.
        streaming_active = deps.streaming_text_notifier is not None
        intermediate_texts: list[str] = []
        if not streaming_active:
            for msg in list(new_msgs):
                if isinstance(msg, ModelResponse):
                    text_parts = [p for p in msg.parts if isinstance(p, TextPart) and p.content]
                    tool_parts = [p for p in msg.parts if isinstance(p, ToolCallPart)]
                    if text_parts and tool_parts:
                        intermediate_texts.append(
                            " ".join(p.content for p in text_parts).strip()
                        )

        # When streaming was active, discard result.output if it duplicates a text that
        # was already streamed (model echoing its own intermediate response after tools).
        if streaming_active and response_text and response_text.strip() in {
            t.strip() for t in streamed_texts
        }:
            response_text = ""

        if ask_question_asked:
            # Discard result.output (echo); use accumulated intermediate texts if any.
            response_text = "\n\n".join(intermediate_texts) if intermediate_texts else ""
        elif intermediate_texts:
            if response_text:
                response_text = "\n\n".join(intermediate_texts) + "\n\n" + response_text
            else:
                response_text = "\n\n".join(intermediate_texts)
        usage = None
        usage_obj = result.usage()
        if usage_obj is not None:
            usage = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0)
                or getattr(usage_obj, "request_tokens", 0)
                or 0,
                "output_tokens": getattr(usage_obj, "output_tokens", 0)
                or getattr(usage_obj, "response_tokens", 0)
                or 0,
            }
        return response_text, list(new_msgs), usage
