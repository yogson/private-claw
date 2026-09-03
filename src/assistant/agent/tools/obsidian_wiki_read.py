"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Read tool for one exact vault file (github.com/AgriciDaniel/claude-obsidian).

obsidian_wiki_search only returns truncated, ranked snippets — not enough to safely
build an obsidian_wiki_write mode="replace" call, which needs the file's full current
content. This tool reads one file by its vault-relative path, in-process, with no
shell and no knowledge of where the vault actually lives on disk required from the
caller. Read-only; never touches the transaction engine or the retrieval index.
"""

import os
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.obsidian_wiki_common import reject_unsafe_vault_path

logger = structlog.get_logger(__name__)

_MAX_READ_BYTES = 1_000_000


def obsidian_wiki_read(ctx: RunContext[TurnDeps], path: str) -> dict[str, Any]:
    """Read the exact current content of one file in the Obsidian vault.

    Use this before obsidian_wiki_write with mode="replace" to get a file's full,
    exact content — obsidian_wiki_search only returns truncated snippets. Never use
    shell tools to read the vault; it lives outside this repository and its path is
    not guaranteed to be discoverable that way.

    Args:
        path: Vault-relative path, e.g. "wiki/index.md". Must not be absolute or
            contain "..".
    """
    vault = os.getenv("CLAUDE_OBSIDIAN_VAULT")
    if not vault:
        return {"status": "unavailable", "reason": "obsidian vault not configured"}

    logger.info("provider.tool_call.obsidian_wiki_read", phase="entry", path=path)

    reason = reject_unsafe_vault_path(path)
    if reason is not None:
        return {"status": "rejected_invalid", "reason": reason}

    target = os.path.join(vault, path)
    if not os.path.isfile(target):
        result = {"status": "not_found", "reason": f"no such file in vault: {path}"}
        logger.info("provider.tool_result.obsidian_wiki_read", status="not_found", path=path)
        return result

    if os.path.getsize(target) > _MAX_READ_BYTES:
        return {"status": "rejected_invalid", "reason": f"file too large to read: {path}"}

    try:
        with open(target, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        result = {"status": "failed", "reason": str(exc)}
        logger.warning("provider.tool_result.obsidian_wiki_read", status="failed", reason=str(exc))
        return result

    logger.info(
        "provider.tool_result.obsidian_wiki_read", status="ok", path=path, content_len=len(content)
    )
    return {"status": "ok", "path": path, "content": content}
