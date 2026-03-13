"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tool registration for Pydantic AI agent.
"""

from pydantic_ai import Agent

from assistant.agent.tools.ask_question import ask_question
from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.memory_propose_update import memory_propose_update
from assistant.agent.tools.memory_search import memory_search


def register_agent_tools(agent: Agent[TurnDeps, str]) -> None:
    """Register runtime tools on the provided agent instance."""
    agent.tool(memory_search)
    agent.tool(memory_propose_update)
    agent.tool(ask_question)
