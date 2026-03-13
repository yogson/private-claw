"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Load prompts from the prompts store directory (markdown files).
"""

import os
from pathlib import Path

_DEFAULT_PROMPTS_DIR = "src/prompts"
_PROMPTS_DIR_ENV_VAR = "ASSISTANT_PROMPTS_DIR"


def resolve_prompts_dir(prompts_dir: str | Path | None = None) -> Path:
    """Resolve the prompts store directory.

    Priority:
    1) explicit function argument
    2) ASSISTANT_PROMPTS_DIR env var
    3) repository default `src/prompts`
    """
    if prompts_dir is not None:
        return Path(prompts_dir)
    env_dir = os.environ.get(_PROMPTS_DIR_ENV_VAR, "").strip()
    if env_dir:
        return Path(env_dir)
    return Path(_DEFAULT_PROMPTS_DIR)


def load_prompt(name: str, prompts_dir: str | Path | None = None) -> str:
    """Load a prompt from the prompts store by name.

    The name is the filename without extension (e.g. 'memory_agent_system'
    loads prompts/memory_agent_system.md). Path traversal in name is rejected.

    Raises:
        ValueError: If name contains path separators or '..'.
        FileNotFoundError: If the prompt file does not exist.
    """
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid prompt name: {name!r}")
    root = resolve_prompts_dir(prompts_dir)
    path = (root / f"{name}.md").resolve()
    root_resolved = root.resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError:
        raise ValueError(f"Prompt name would escape prompts directory: {name!r}") from None
    return path.read_text(encoding="utf-8").strip()
