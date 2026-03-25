"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

write_markdown_file tool for the Pydantic AI agent.
Writes (creates or overwrites) a markdown file at the given path.
Writing outside the user's home directory is forbidden.
"""

from pathlib import Path
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)


def write_markdown_file(
    ctx: RunContext[TurnDeps],
    file_path: str,
    content: str,
) -> dict[str, Any]:
    """Write (create or overwrite) a markdown (.md) file at file_path with content.

    Parent directories are created automatically.
    Writing outside the user's home directory is forbidden.
    """
    logger.info(
        "provider.tool_call.write_markdown_file",
        phase="entry",
        file_path=file_path,
        content_length=len(content),
    )

    result = _validate_and_write_markdown(file_path=file_path, content=content)

    logger.info(
        "provider.tool_result.write_markdown_file",
        status=result.get("status"),
        path=result.get("path"),
        reason=result.get("reason"),
    )
    return result


def _validate_and_write_markdown(file_path: str, content: str) -> dict[str, Any]:
    """Validate path and content, then write a markdown file.

    Returns a result dict with 'status' and either 'path' (on success) or 'reason' (on error).
    """
    # Must end with .md
    if not file_path.strip().endswith(".md"):
        return {
            "status": "rejected_invalid",
            "reason": f"file_path must have a .md extension, got: {file_path!r}",
        }

    resolved = Path(file_path).expanduser().resolve()

    # Security: only allow writes inside the user's home directory
    home = Path.home().resolve()
    try:
        resolved.relative_to(home)
        # If relative_to succeeds the path is inside home — allow it
    except ValueError:
        # Not inside home — deny it
        return {
            "status": "rejected_forbidden",
            "reason": (
                f"writing outside the user home directory is not allowed (resolved path: {resolved})"
            ),
        }

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")

    return {
        "status": "ok",
        "path": str(resolved),
    }
