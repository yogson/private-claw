"""
System prompt composition for the pydantic_ai agent.

Builds the agent's system prompt by combining the base memory agent prompt
with any enabled capability prompts from the runtime config.
"""

from pathlib import Path

from assistant.agent.constants import MEMORY_AGENT_PROMPT_NAME
from assistant.core.capabilities.loader import load_capability_definitions
from assistant.core.config.loader import resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig
from assistant.core.prompts import load_prompt


def _config_dir(config: RuntimeConfig) -> Path:
    """Resolve config dir from RuntimeConfig or fallback to env/default."""
    return config.config_dir if config.config_dir is not None else resolve_config_dir()


def _compose_system_prompt(config: RuntimeConfig) -> str:
    """Compose system prompt from base + enabled capability prompts."""
    base = load_prompt(MEMORY_AGENT_PROMPT_NAME)
    policy = config.capabilities
    denied = frozenset(policy.denied_capabilities)
    enabled = frozenset(policy.enabled_capabilities)
    definitions = load_capability_definitions(config_dir=_config_dir(config))
    parts = [base]
    for cap_id in sorted(enabled):
        if cap_id in denied:
            continue
        definition = definitions.get(cap_id)
        if definition and definition.prompt.strip():
            parts.append(definition.prompt.strip())
    return "\n\n".join(parts)
