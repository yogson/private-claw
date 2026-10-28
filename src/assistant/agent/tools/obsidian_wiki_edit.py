"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Find-and-replace edit tool for one existing vault file — the vault analogue of the
Edit tool. Reads the current content, applies one exact string replacement, and hands
the result to obsidian_wiki_write's transaction pipeline (same safety guarantees: hash
preconditions, auto-reindex). Exists so a small change to a large note doesn't require
reading and retyping the whole file through obsidian_wiki_write(mode="replace").
"""

import hashlib
import os
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.obsidian_wiki_common import reject_unsafe_vault_path
from assistant.agent.tools.obsidian_wiki_write import write_vault_transaction

logger = structlog.get_logger(__name__)


def obsidian_wiki_edit(
    ctx: RunContext[TurnDeps],
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    operation_type: str = "markdown",
) -> dict[str, Any]:
    """Edit part of an existing vault file by exact string replacement.

    Use this instead of obsidian_wiki_write(mode="replace") when changing a small part
    of a large file — no need to obsidian_wiki_read and retype the whole file, just the
    part that changes. old_string must match exactly (including whitespace) and, unless
    replace_all=True, must occur exactly once in the file — include enough surrounding
    context to make it unique.

    Args:
        path: Vault-relative path of the file to edit.
        old_string: Exact text to find. Must be unique in the file unless replace_all.
        new_string: Text to replace it with. Must differ from old_string.
        replace_all: Replace every occurrence instead of requiring exactly one.
        operation_type: Vault operation type for the resulting transaction; "markdown"
            (direct note edit) fits most edits. Confined to wiki/ either way.
    """
    vault = os.getenv("CLAUDE_OBSIDIAN_VAULT")
    if not vault:
        return {"status": "unavailable", "reason": "obsidian vault not configured"}

    logger.info("provider.tool_call.obsidian_wiki_edit", phase="entry", path=path)

    reason = reject_unsafe_vault_path(path)
    if reason is not None:
        return {"status": "rejected_invalid", "reason": reason}
    if not old_string:
        return {"status": "rejected_invalid", "reason": "old_string must not be empty"}
    if old_string == new_string:
        return {"status": "rejected_invalid", "reason": "old_string and new_string are identical"}

    target = os.path.join(vault, path)
    if not os.path.isfile(target):
        return {
            "status": "rejected_invalid",
            "reason": (
                f"no such file in vault (use obsidian_wiki_write mode=create instead): {path}"
            ),
        }

    with open(target, encoding="utf-8") as f:
        content = f.read()

    count = content.count(old_string)
    if count == 0:
        return {"status": "rejected_invalid", "reason": f"old_string not found in {path}"}
    if not replace_all and count > 1:
        return {
            "status": "rejected_invalid",
            "reason": (
                f"old_string is not unique in {path} ({count} occurrences) — include "
                "more surrounding context, or pass replace_all=True"
            ),
        }

    new_content = (
        content.replace(old_string, new_string)
        if replace_all
        else content.replace(old_string, new_string, 1)
    )

    # Pin the precondition to the content this edit was computed from, so a
    # concurrent change after our read surfaces as a conflict instead of being lost.
    return write_vault_transaction(
        ctx,
        operation_type=operation_type,
        writes=[{"path": path, "mode": "replace", "content": new_content}],
        base_hashes={path: hashlib.sha256(content.encode("utf-8")).hexdigest()},
    )
