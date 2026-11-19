"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

System prompt composition for the pydantic_ai agent.

The static system prompt (base memory agent prompt + active capability prompts + the
loadable-capability index) is built from one capability snapshot and cached with the
adapter; it must stay byte-stable so the Anthropic instruction cache keeps hitting. Per-turn
facts such as the current date are delivered separately as a dynamic instruction (see
:func:`current_date_instruction`).

Layout::

    <base prompt>
    <prompts of base capabilities, sorted by id>
    <prompts of session-loaded capabilities (and their nested children), in load order>
    ## Loadable capabilities          (only when the index is non-empty)
    ...

Loaded prompts are appended after the base ones, so loading a capability leaves the base
prefix of the prompt byte-identical.
"""

from collections.abc import Mapping
from datetime import datetime

from assistant.agent.constants import MEMORY_AGENT_PROMPT_NAME
from assistant.core.capabilities.resolver import EffectiveCapabilities
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.prompts import load_prompt

INDEX_HEADING = "## Loadable capabilities"


def _compose_system_prompt(
    definitions: Mapping[str, CapabilityDefinition],
    effective: EffectiveCapabilities,
    *,
    index_enabled: bool = True,
) -> str:
    """Compose the static system prompt (see the module docstring for the layout).

    ``index_enabled`` is False when dynamic loading is off (no capability_load tool); the
    index section is then omitted.
    """
    parts = [load_prompt(MEMORY_AGENT_PROMPT_NAME)]
    for cap_id in [*sorted(effective.base), *effective.added_by_loading]:
        definition = definitions.get(cap_id)
        if definition and definition.prompt.strip():
            parts.append(definition.prompt.strip())
    if index_enabled:
        index = render_capability_index(definitions, effective)
        if index:
            parts.append(index)
    return "\n\n".join(parts)


def render_capability_index(
    definitions: Mapping[str, CapabilityDefinition], effective: EffectiveCapabilities
) -> str:
    """The "Loadable capabilities" section (ids + descriptions), or "" when empty.

    Deterministic: sorted by id, no tool definitions. When the session has loaded
    capabilities, a closing line names them and points at capability_unload.
    """
    if not effective.index:
        return ""
    lines = [
        INDEX_HEADING,
        "Call capability_load(capability_id, reason) when a request needs one of these:",
    ]
    for cap_id in effective.index:
        definition = definitions.get(cap_id)
        description = definition.description if definition is not None else ""
        lines.append(f"- {cap_id} — {description}" if description else f"- {cap_id}")
    if effective.loaded:
        lines.append(
            f"Loaded for this session: {', '.join(effective.loaded)}. Call "
            "capability_unload(capability_id) when one is no longer needed."
        )
    return "\n".join(lines)


def current_date_instruction(now: datetime) -> str:
    """Per-turn instruction carrying the current date (never baked into the cached prompt)."""
    return f"Today's date is {now.strftime('%B %d, %Y')}."
