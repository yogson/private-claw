"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tool registration for Pydantic AI agent.
"""

from collections.abc import Sequence
from typing import Any

import structlog

from assistant.agent.tools.ask_question import ask_question
from assistant.agent.tools.memory_propose_update import memory_propose_update
from assistant.agent.tools.memory_search import memory_search
from assistant.agent.tools.tavily_search import get_tavily_search_tool

logger = structlog.get_logger(__name__)


def get_agent_tools() -> Sequence[Any]:
    """Return tools for the agent. Pass to Agent(tools=...). Accepts functions and Tool objects."""
    tools: list[Any] = [memory_search, memory_propose_update, ask_question]
    tavily_tool = get_tavily_search_tool()
    if tavily_tool is not None:
        tools.append(tavily_tool)
    else:
        logger.info(
            "provider.tools.tavily_skipped",
            reason="TAVILY_API_KEY not set",
            hint="Set TAVILY_API_KEY to enable web search",
        )
    return tools
