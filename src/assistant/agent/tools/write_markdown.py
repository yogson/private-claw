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
    ctx: RunContext[TurnDeps], file_path: str, content: str, append: bool = False
) -> dict[str, Any]:
    """Write (create or overwrite) a markdown (.md) file at file_path with content.

        Args:
        file_path: Full path to the markdown file to write. Must have .md extension.
        content: String content to write.
        append: Set to True to append the content to the file end. Defaults to False.

    Parent directories are created automatically.
    Writing outside the user's home directory is forbidden.
    """
    logger.info(
        "provider.tool_call.write_markdown_file",
        phase="entry",
        file_path=file_path,
        content_length=len(content),
    )

    resolved = Path(file_path).expanduser().resolve()
    validation_error = _is_invalid_path(resolved)
    if validation_error:
        return validation_error

    if append:
        if not resolved.exists():
            return {
                "status": "rejected_invalid",
                "reason": "file does not exist",
            }
        content = _prepend_content(resolved, content)

    result = _write_markdown(file_path=resolved, content=content)

    logger.info(
        "provider.tool_result.write_markdown_file",
        status=result.get("status"),
        path=result.get("path"),
        reason=result.get("reason"),
    )
    return result


def _prepend_content(file_path: Path, content: str) -> str:
    existent_content = file_path.read_text(encoding="utf-8")
    return existent_content + "\n" + content


def _is_invalid_path(file_path: Path) -> dict[str, Any] | None:

    # Must end with .md
    if not file_path.suffix.endswith(".md"):
        return {
            "status": "rejected_invalid",
            "reason": f"file_path must have a .md extension, got: {file_path!r}",
        }

    # Security: only allow writes inside the user's home directory
    home = Path.home().resolve()
    try:
        file_path.relative_to(home)
        # If relative_to succeeds the path is inside home — allow it
    except ValueError:
        # Not inside home — deny it
        return {
            "status": "rejected_forbidden",
            "reason": (
                f"writing outside the user home directory is not allowed (resolved path: {file_path})"
            ),
        }


def _write_markdown(file_path: Path, content: str) -> dict[str, Any]:
    """Validate path and content, then write a markdown file.

    Returns a result dict with 'status' and either 'path' (on success) or 'reason' (on error).
    """

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    return {
        "status": "ok",
        "path": str(file_path),
    }
