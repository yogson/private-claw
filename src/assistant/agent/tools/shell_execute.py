"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Shell execution tools with policy gates: readonly (built-in allowlist) and
allowlisted (config-driven command_allowlist with command_pattern,
allowed_args_pattern, max_timeout_seconds).
"""

import re
import shlex
import subprocess
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.core.config.schemas import CommandAllowlistEntry

logger = structlog.get_logger(__name__)

_MAX_TIMEOUT_SECONDS = 30


def _parse_command(command: str) -> tuple[str, list[str]]:
    """Parse command string into (executable, args). Returns ('', []) on parse error."""
    try:
        parts = shlex.split(command)
        if not parts:
            return "", []
        return parts[0], parts[1:]
    except ValueError:
        return "", []


def shell_execute_readonly(
    ctx: RunContext[TurnDeps],
    command: str,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Execute a read-only shell command.

    Allowed commands come from config capabilities.shell_readonly_commands.
    """
    readonly_commands = getattr(ctx.deps, "shell_readonly_commands", None) or []
    allowed_set = frozenset(
        c.strip().lower() for c in readonly_commands if isinstance(c, str) and c.strip()
    )
    if not allowed_set:
        logger.info(
            "provider.tool_call.shell_execute_readonly",
            status="rejected_unavailable",
            reason="shell_readonly_commands is empty",
        )
        return {
            "status": "rejected_unavailable",
            "reason": "shell_readonly_commands is empty",
            "stdout": "",
            "stderr": "",
        }
    executable, args = _parse_command(command.strip())
    if not executable:
        logger.info(
            "provider.tool_call.shell_execute_readonly",
            status="rejected_invalid",
            reason="empty or invalid command",
        )
        return {
            "status": "rejected_invalid",
            "reason": "empty or invalid command",
            "stdout": "",
            "stderr": "",
        }
    exe_lower = executable.lower()
    if exe_lower not in allowed_set:
        logger.info(
            "provider.tool_call.shell_execute_readonly",
            status="rejected_not_allowed",
            executable=executable,
            allowed=list(allowed_set),
        )
        return {
            "status": "rejected_not_allowed",
            "reason": f"'{executable}' is not in readonly allowlist",
            "stdout": "",
            "stderr": "",
        }
    bounded_timeout = max(1, min(timeout_seconds, _MAX_TIMEOUT_SECONDS))
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=bounded_timeout,
        )
        logger.info(
            "provider.tool_call.shell_execute_readonly",
            status="ok",
            executable=executable,
            returncode=result.returncode,
        )
        return {
            "status": "ok",
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }
    except subprocess.TimeoutExpired:
        logger.warning(
            "provider.tool_call.shell_execute_readonly",
            status="timeout",
            executable=executable,
            timeout=bounded_timeout,
        )
        return {
            "status": "timeout",
            "reason": f"command timed out after {bounded_timeout}s",
            "stdout": "",
            "stderr": "",
        }
    except Exception as exc:
        logger.warning(
            "provider.tool_call.shell_execute_readonly",
            status="failed",
            executable=executable,
            error=str(exc),
        )
        return {"status": "failed", "reason": str(exc), "stdout": "", "stderr": ""}


def _find_matching_entry(
    executable: str, args: list[str], allowlist: list[CommandAllowlistEntry]
) -> CommandAllowlistEntry | None:
    """Return matching allowlist entry if command and args match a template."""
    exe_lower = executable.lower()
    args_str = " ".join(args)
    for entry in allowlist:
        if exe_lower != entry.command_pattern.strip().lower():
            continue
        try:
            if re.fullmatch(entry.allowed_args_pattern, args_str):
                return entry
        except re.error:
            continue
    return None


def shell_execute_allowlisted(
    ctx: RunContext[TurnDeps],
    command: str,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Execute a shell command from config command_allowlist. Deny-by-default.

    Matches command_pattern and allowed_args_pattern (regex). Uses per-entry
    max_timeout_seconds cap.
    """
    allowlist = getattr(ctx.deps, "shell_command_allowlist", None) or []
    if not allowlist:
        logger.info(
            "provider.tool_call.shell_execute_allowlisted",
            status="rejected_unavailable",
            reason="command_allowlist is empty",
        )
        return {
            "status": "rejected_unavailable",
            "reason": "command_allowlist is empty",
            "stdout": "",
            "stderr": "",
        }
    executable, args = _parse_command(command.strip())
    if not executable:
        logger.info(
            "provider.tool_call.shell_execute_allowlisted",
            status="rejected_invalid",
            reason="empty or invalid command",
        )
        return {
            "status": "rejected_invalid",
            "reason": "empty or invalid command",
            "stdout": "",
            "stderr": "",
        }
    entry = _find_matching_entry(executable, args, allowlist)
    if entry is None:
        logger.info(
            "provider.tool_call.shell_execute_allowlisted",
            status="rejected_not_allowed",
            executable=executable,
        )
        return {
            "status": "rejected_not_allowed",
            "reason": f"'{executable}' or args do not match any command_allowlist template",
            "stdout": "",
            "stderr": "",
        }
    bounded_timeout = max(1, min(timeout_seconds, entry.max_timeout_seconds, _MAX_TIMEOUT_SECONDS))
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=bounded_timeout,
        )
        logger.info(
            "provider.tool_call.shell_execute_allowlisted",
            status="ok",
            executable=executable,
            returncode=result.returncode,
        )
        return {
            "status": "ok",
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }
    except subprocess.TimeoutExpired:
        logger.warning(
            "provider.tool_call.shell_execute_allowlisted",
            status="timeout",
            executable=executable,
            timeout=bounded_timeout,
        )
        return {
            "status": "timeout",
            "reason": f"command timed out after {bounded_timeout}s",
            "stdout": "",
            "stderr": "",
        }
    except Exception as exc:
        logger.warning(
            "provider.tool_call.shell_execute_allowlisted",
            status="failed",
            executable=executable,
            error=str(exc),
        )
        return {"status": "failed", "reason": str(exc), "stdout": "", "stderr": ""}
