"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tavily web search tool for Pydantic AI agent.
Uses TAVILY_API_KEY from environment. Tool is only registered when key is set.
"""

import os

from pydantic_ai.common_tools.tavily import tavily_search_tool


def get_tavily_search_tool():
    """Return Tavily search tool if TAVILY_API_KEY is set, else None."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return None
    return tavily_search_tool(api_key, max_results=5)
