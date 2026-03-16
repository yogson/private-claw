"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Tool registration for Pydantic AI agent. Tools are gated by capability policy:
only tools whose capability_id is in allowed_capabilities and not in denied_capabilities
are registered. Fail closed when capability is not allowed.
"""

from collections.abc import Sequence

import structlog
from pydantic_ai import Tool
from pydantic_ai.tools import ToolFuncEither

from assistant.agent.tools.ask_question import ask_question
from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.memory_propose_update import memory_propose_update
from assistant.agent.tools.memory_search import memory_search
from assistant.agent.tools.shell_execute import shell_execute_allowlisted, shell_execute_readonly
from assistant.agent.tools.tavily_search import get_tavily_search_tool
from assistant.core.config.schemas import CapabilitiesConfig, RuntimeConfig

logger = structlog.get_logger(__name__)

type AgentTool = Tool[TurnDeps] | ToolFuncEither[TurnDeps, ...]

_TOOL_CAPABILITY_MAP: dict[AgentTool, str] = {
    memory_search: "cap.memory.read",
    memory_propose_update: "cap.memory.update",
    ask_question: "cap.assistant.ask",  # core UX, always allowed when in allowed list
    shell_execute_readonly: "cap.shell.execute.readonly",
    shell_execute_allowlisted: "cap.shell.execute.allowlisted",
}


def _is_capability_allowed(cap_id: str, caps: CapabilitiesConfig) -> bool:
    """Return True if capability is explicitly allowed and not denied."""
    if cap_id in caps.denied_capabilities:
        return False
    return cap_id in caps.allowed_capabilities


def get_agent_tools(config: RuntimeConfig) -> Sequence[AgentTool]:
    """Return tools for the agent, gated by capability policy. Fail closed."""
    caps = config.capabilities
    tools: list[AgentTool] = []
    for tool, cap_id in _TOOL_CAPABILITY_MAP.items():
        if _is_capability_allowed(cap_id, caps):
            tools.append(tool)
        else:
            logger.debug(
                "provider.tools.skipped",
                capability_id=cap_id,
                reason="not in allowed_capabilities or in denied_capabilities",
            )
    tavily_tool = get_tavily_search_tool()
    if tavily_tool is not None and _is_capability_allowed("cap.web.search.query", caps):
        tools.append(tavily_tool)
    elif tavily_tool is not None:
        logger.debug(
            "provider.tools.tavily_skipped",
            capability_id="cap.web.search.query",
            reason="not in allowed_capabilities or in denied_capabilities",
        )
    elif _is_capability_allowed("cap.web.search.query", caps):
        logger.info(
            "provider.tools.tavily_skipped",
            reason="TAVILY_API_KEY not set",
            hint="Set TAVILY_API_KEY to enable web search",
        )
    return tools
