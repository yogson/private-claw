"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Pydantic AI Agent runtime for turn execution with typed memory_propose_update tool.
Replaces manual provider tool protocol handling with Agent-managed tool loop.
"""

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from assistant.agent.ask_question_extractor import _extract_pending_ask_question
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
    "TurnDeps",
    "_extract_pending_ask_question",
    "_new_messages_to_plans",
    "_new_messages_to_session_records",
    "_llm_messages_to_history",
    "_normalize_candidate_for_upsert",
]


def _normalize_candidate_for_upsert(candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Backward-compatible wrapper for existing tests/importers."""
    return normalize_candidate_for_upsert(candidate)


def _create_agent(model_id: str, system_prompt: str, config: RuntimeConfig) -> Agent[TurnDeps, str]:
    """Create Agent with memory_propose_update tool. Output type is str (final assistant text)."""
    return Agent(
        model_id,
        deps_type=TurnDeps,
        system_prompt=system_prompt,
        retries=0,
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
        """
        from pydantic_ai._agent_graph import CallToolsNode  # type: ignore[import]
        from pydantic_ai.messages import ToolCallPart

        if not messages:
            return "", [], None
        effective_model = model_id if model_id else self._model_id
        if effective_model and not effective_model.startswith("anthropic:"):
            effective_model = f"anthropic:{effective_model}"
        user_prompt = _message_to_user_prompt_content(messages[-1])
        history_msgs = messages[:-1] if len(messages) > 1 else []
        history = _llm_messages_to_history(history_msgs)
        model_settings = {"max_tokens": self._max_tokens}
        async with self._agent.iter(  # type: ignore[call-overload]
            user_prompt,
            message_history=history,
            deps=deps,
            model=effective_model or self._model_id,
            model_settings=model_settings,
        ) as agent_run:
            async for node in agent_run:
                if isinstance(node, CallToolsNode) and deps.tool_call_notifier is not None:
                    for part in node.model_response.parts:
                        if isinstance(part, ToolCallPart):
                            try:
                                await deps.tool_call_notifier(
                                    part.tool_name, part.args_as_json_str()
                                )
                            except Exception:
                                pass
        result = agent_run.result
        if result is None:
            return "", [], None
        response_text = result.output
        new_msgs = result.new_messages()
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
