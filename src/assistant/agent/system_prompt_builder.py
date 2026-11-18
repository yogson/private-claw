"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

System prompt composition for the pydantic_ai agent.

The static system prompt (base memory agent prompt + active capability prompts) is built
from one capability snapshot and cached with the adapter; it must stay byte-stable so the
Anthropic instruction cache keeps hitting. Per-turn facts such as the current date are
delivered separately as a dynamic instruction (see :func:`current_date_instruction`).
"""

from datetime import datetime

from assistant.agent.constants import MEMORY_AGENT_PROMPT_NAME
from assistant.agent.tools.registry import ActiveCapabilities
from assistant.core.prompts import load_prompt


def _compose_system_prompt(active: ActiveCapabilities) -> str:
    """Compose the static system prompt from base + active capability prompts."""
    definitions, enabled = active
    parts = [load_prompt(MEMORY_AGENT_PROMPT_NAME)]
    for cap_id in sorted(enabled):
        definition = definitions.get(cap_id)
        if definition and definition.prompt.strip():
            parts.append(definition.prompt.strip())
    return "\n\n".join(parts)


def current_date_instruction(now: datetime) -> str:
    """Per-turn instruction carrying the current date (never baked into the cached prompt)."""
    return f"Today's date is {now.strftime('%B %d, %Y')}."
