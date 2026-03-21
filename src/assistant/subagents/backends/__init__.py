"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Delegation backend adapters.
"""

from assistant.subagents.backends.claude_code import ClaudeCodeBackendAdapter
from assistant.subagents.backends.claude_code_streaming import ClaudeCodeStreamingBackendAdapter

__all__ = ["ClaudeCodeBackendAdapter", "ClaudeCodeStreamingBackendAdapter"]
