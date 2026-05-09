"""
Component ID: CMP_CORE_CHAT_COMPACTION

Chat compaction service for token-limited session summarization.

Uses a cheaper LLM (claude-haiku-4-5 by default) to summarize chat history
when session token usage exceeds a configurable threshold.
"""

import structlog
from pydantic_ai import Agent

from assistant.agent.interfaces import LLMMessage

logger = structlog.get_logger(__name__)

SUMMARIZER_PROMPT = """\
You are a conversation summarizer. Analyze the chat history and provide a concise \
continuation briefing covering:
1. What was being discussed (main topics/goals)
2. What was accomplished (completed actions, decisions made)
3. What remains pending or planned
4. Any important context the user would need to continue seamlessly

Be concise but preserve critical details like names, IDs, specific values, and \
any commitments made. Format as a structured briefing.\
"""


class ChatCompactionService:
    """Summarizes chat history using a lightweight LLM agent."""

    def __init__(self, model_id: str, max_tokens: int = 2048) -> None:
        self._agent: Agent[None, str] = Agent(
            f"anthropic:{model_id}",  # type: ignore[arg-type]
            system_prompt=SUMMARIZER_PROMPT,
            result_type=str,
        )
        self._max_tokens = max_tokens

    async def summarize(
        self,
        messages: list[LLMMessage],
        trace_id: str,
    ) -> str:
        """Generate a summary from message history.

        Args:
            messages: The full LLM message history to summarize.
            trace_id: Correlation ID for observability.

        Returns:
            A concise summary string suitable for injection as compaction context.
        """
        formatted = self._format_for_summary(messages)
        logger.info(
            "compaction.summarize_start",
            trace_id=trace_id,
            message_count=len(messages),
            input_chars=len(formatted),
        )
        result = await self._agent.run(formatted)
        summary = result.data
        logger.info(
            "compaction.summarize_complete",
            trace_id=trace_id,
            summary_chars=len(summary),
        )
        return summary

    @staticmethod
    def _format_for_summary(messages: list[LLMMessage]) -> str:
        """Format LLM messages into a text representation for the summarizer."""
        parts: list[str] = []
        for msg in messages:
            role = msg.role.value.upper()
            content = msg.content or ""
            if msg.content_blocks:
                block_texts: list[str] = []
                for block in msg.content_blocks:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            block_texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            block_texts.append(f"[Tool call: {block.get('name', '?')}]")
                        elif block.get("type") == "tool_result":
                            block_texts.append(f"[Tool result for {block.get('tool_name', '?')}]")
                if block_texts:
                    content = "\n".join(block_texts)
            if content.strip():
                parts.append(f"{role}: {content.strip()}")
        return "\n\n".join(parts)
