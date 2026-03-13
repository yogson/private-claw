"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Prompt store utilities for loading system prompts from markdown files.
"""

from assistant.core.prompts.loader import load_prompt, resolve_prompts_dir

__all__ = ["load_prompt", "resolve_prompts_dir"]
