"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Directory listing tool for the Obsidian vault (github.com/AgriciDaniel/claude-obsidian).

obsidian_wiki_search needs a query and returns ranked snippets, not a folder listing —
there was no way to ask "what's in wiki/projects/<slug>/" directly. This tool does one
non-recursive listing of a vault-relative directory, in-process, read-only.
"""

import os
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.obsidian_wiki_common import reject_unsafe_vault_path

logger = structlog.get_logger(__name__)


def obsidian_wiki_list(ctx: RunContext[TurnDeps], path: str = "") -> dict[str, Any]:
    """List the files and subfolders directly inside one vault directory (non-recursive).

    Use this to see what already exists in a folder — e.g. wiki/projects/<slug>/ before
    deciding whether to create or replace a note — rather than guessing from search
    snippets. Omit path (or pass "") to list the vault root.

    Args:
        path: Vault-relative directory path, e.g. "wiki/projects/automation/metalang".
            Must not be absolute or contain "..".
    """
    vault = os.getenv("CLAUDE_OBSIDIAN_VAULT")
    if not vault:
        return {"status": "unavailable", "reason": "obsidian vault not configured"}

    logger.info("provider.tool_call.obsidian_wiki_list", phase="entry", path=path)

    if path:
        reason = reject_unsafe_vault_path(path)
        if reason is not None:
            return {"status": "rejected_invalid", "reason": reason}

    target = os.path.join(vault, path) if path else vault
    if not os.path.isdir(target):
        result = {"status": "not_found", "reason": f"no such directory in vault: {path or '.'}"}
        logger.info("provider.tool_result.obsidian_wiki_list", status="not_found", path=path)
        return result

    try:
        names = sorted(os.listdir(target))
    except OSError as exc:
        result = {"status": "failed", "reason": str(exc)}
        logger.warning("provider.tool_result.obsidian_wiki_list", status="failed", reason=str(exc))
        return result

    entries = [
        {"name": name, "type": "dir" if os.path.isdir(os.path.join(target, name)) else "file"}
        for name in names
    ]
    logger.info(
        "provider.tool_result.obsidian_wiki_list", status="ok", path=path, entry_count=len(entries)
    )
    return {"status": "ok", "path": path or ".", "entries": entries}
