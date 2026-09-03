"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Shared helpers for the obsidian_wiki_* tools (github.com/AgriciDaniel/claude-obsidian).
"""

from pathlib import PurePosixPath


def reject_unsafe_vault_path(path: str) -> str | None:
    """Return a reason string if a vault-relative path is unsafe, else None."""
    if not path or not path.strip():
        return "path must not be empty"
    rel = PurePosixPath(path)
    if rel.is_absolute():
        return f"path must be vault-relative, not absolute: {path}"
    if ".." in rel.parts:
        return f"path must not contain '..': {path}"
    return None
