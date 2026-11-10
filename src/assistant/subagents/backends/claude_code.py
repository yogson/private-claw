"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code backend adapter for delegated staged execution.
"""

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import structlog

from assistant.core.config.schemas import SubagentPermissionMode
from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.interfaces import DelegationBackendAdapterInterface
from assistant.subagents.permission_modes import (
    PermissionModeMonitor,
    StreamEventKind,
    explain_cli_rejection,
    resolve_permission_mode,
)

logger = structlog.get_logger(__name__)

# stream-json message types that carry model output; anything else that is not
# "system" (rate_limit_event, stream_event, ...) does not end the mode check.
_MODEL_OUTPUT_TYPES = frozenset({"assistant", "user", "result"})


def _stream_event_kind(message_type: object) -> StreamEventKind:
    if message_type == "system":
        return "system"
    if message_type in _MODEL_OUTPUT_TYPES:
        return "model_output"
    return "other"


_DEFAULT_CLAUDE_BINARY = "claude"
# StreamReader's default per-line buffer is 64KiB (asyncio.create_subprocess_exec's
# own default); --output-format stream-json can legitimately put a large tool_use/
# tool_result payload (a big file read or diff) on a single line, which would
# otherwise raise asyncio.LimitOverrunError and get misreported as a run failure.
_STDIO_STREAM_LIMIT = 10 * 1024 * 1024

# Activity-log formatting reuses claude-agent-sdk's own message parser/types
# so a --output-format stream-json CLI line produces the same log line as the
# SDK-native claude_code_streaming.py backend. This is soft: parsing and
# result extraction below use the raw stream-json dict directly (see
# _drain_stdout), so a missing/incompatible SDK only disables pretty logging,
# never core execution.
try:
    from claude_agent_sdk._internal.message_parser import parse_message

    from assistant.subagents.backends.log_formatting import write_log_lines

    _LOG_PARSING_AVAILABLE = True
except ImportError:
    _LOG_PARSING_AVAILABLE = False


class ClaudeCodeBackendAdapter(DelegationBackendAdapterInterface):
    """Executes staged delegation tasks via local Claude Code CLI."""

    def __init__(
        self,
        binary: str = _DEFAULT_CLAUDE_BINARY,
        mcp_servers: dict[str, Any] | None = None,
    ) -> None:
        self._binary = binary
        # MCP servers exposed to every sub-agent run, passed as inline --mcp-config
        # JSON.  The CLI ignores the "mcpServers" key in settings.json, so that file
        # cannot be used to deliver MCP servers to sub-agents.
        self._mcp_servers = mcp_servers or {}

    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute(self, request: DelegationRun) -> DelegationResult:
        prompt = self._build_prompt(request)
        permission_mode = resolve_permission_mode(
            request.backend_params.get("permission_mode"),
            source="claude_code",
            task_id=request.task_id,
        )
        cmd = self._build_command(request, prompt, permission_mode)
        cwd = request.backend_params.get("directory")
        log_path = Path(request.log_path) if request.log_path else None
        process: asyncio.subprocess.Process | None = None
        result_line: dict[str, Any] | None = None
        mode_monitor = PermissionModeMonitor(permission_mode, request.task_id, request.model_id)
        mode_error: str | None = None
        raw_lines: list[str] = []
        stderr_chunks: list[bytes] = []

        async def _drain_stdout(proc: asyncio.subprocess.Process) -> None:
            nonlocal result_line, mode_error
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                raw_lines.append(line)
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "result":
                    result_line = data
                mode_error = mode_monitor.observe(_stream_event_kind(data.get("type")), data)
                if log_path is not None:
                    await self._log_stream_json_line(log_path, data, request.task_id)
                if mode_error is not None:
                    # Never let the run continue in a mode nobody asked for
                    # (e.g. Manual instead of an unavailable auto mode).
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    return

        async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
            assert proc.stderr is not None
            async for chunk in proc.stderr:
                stderr_chunks.append(chunk)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                limit=_STDIO_STREAM_LIMIT,
            )
            await asyncio.wait_for(
                asyncio.gather(
                    _drain_stdout(process),
                    _drain_stderr(process),
                    process.wait(),
                ),
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            # wait_for cancels the gathered awaitables on the inner timeout but
            # does not touch the OS process itself, so without an explicit kill
            # the claude CLI subprocess orphans and keeps running/consuming
            # resources after we've already reported the run as timed out.
            await self._kill(process)
            return DelegationResult(ok=False, error="claude run timed out")
        except asyncio.CancelledError:
            # Deliberate cancellation (coordinator.cancel_task) - stop the
            # subprocess before propagating so cancel actually halts work
            # instead of leaving it running unattended.
            await self._kill(process)
            raise
        except FileNotFoundError:
            # cwd is validated before execution, so this means the binary is missing
            return DelegationResult(ok=False, error="claude CLI binary not found")
        except Exception as exc:  # pragma: no cover - defensive runtime branch
            await self._kill(process)
            return DelegationResult(ok=False, error=f"claude execution failed: {exc}")

        assert process is not None
        if mode_error is not None:
            return DelegationResult(ok=False, error=mode_error)
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            # A structured result line, if we got one before the process died,
            # is more informative than raw stderr - e.g. an API error surfaced
            # as {"type":"result","is_error":true,"result":"..."} right before
            # a nonzero exit.
            result_error = result_line.get("result") if result_line else None
            msg = (
                (str(result_error) if result_error else None)
                or explain_cli_rejection(permission_mode, stderr_text)
                or stderr_text
                or f"claude exited with code {process.returncode}"
            )
            return DelegationResult(ok=False, error=msg)

        if result_line is None:
            # --output-format stream-json produced no {"type":"result",...} line
            # even though the process exited 0 - fall back to whatever raw text
            # it did print (e.g. an incompatible/older CLI not honoring the flag)
            # rather than reporting an otherwise-successful run as failed.
            fallback = "\n".join(raw_lines).strip()
            if fallback:
                return DelegationResult(ok=True, output_text=fallback)
            return DelegationResult(
                ok=False,
                error="claude run ended without a result line (--output-format stream-json)",
            )

        usage = result_line.get("usage") or {}
        if result_line.get("is_error"):
            error_text = result_line.get("result")
            return DelegationResult(
                ok=False,
                error=str(error_text) if error_text else "claude reported an error",
                usage=usage,
            )
        output = result_line.get("result")
        output_text = str(output) if output is not None else ""
        return DelegationResult(ok=True, output_text=output_text, usage=usage)

    @staticmethod
    async def _log_stream_json_line(log_path: Path, data: dict[str, Any], task_id: str) -> None:
        # Logging is observability, not part of the run itself - any failure
        # here (parsing or formatting) must never fail the delegated task.
        # Catching broad Exception, not just the parser's own error type,
        # matters because this reaches into claude_agent_sdk's private
        # _internal.message_parser module - an API change there could raise
        # something other than its documented error type.
        if not _LOG_PARSING_AVAILABLE:
            return
        try:
            msg = parse_message(data)
        except Exception:
            logger.exception("subagent.log.parse_failed", task_id=task_id)
            return
        if msg is None:
            return
        await write_log_lines(log_path, msg, task_id)

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    def _build_command(
        self,
        request: DelegationRun,
        prompt: str,
        permission_mode: SubagentPermissionMode,
    ) -> list[str]:
        cmd = [
            self._binary,
            "-p",
            prompt,
            "--model",
            request.model_id,
            "--max-turns",
            str(request.max_turns),
            "--output-format",
            "stream-json",
            "--verbose",
            # Always explicit: without it a headless run uses the settings
            # default (Manual), which denies everything outside the allow list.
            "--permission-mode",
            permission_mode,
        ]
        if self._mcp_servers:
            cmd += ["--mcp-config", json.dumps({"mcpServers": self._mcp_servers})]
        effort = str(request.backend_params.get("effort", "")).strip()
        if effort:
            cmd += ["--effort", effort]
        add_dirs = request.backend_params.get("add_dirs")
        if isinstance(add_dirs, list):
            for item in add_dirs:
                if isinstance(item, str) and item.strip():
                    cmd += ["--add-dir", item.strip()]
        plugin_dirs = request.backend_params.get("plugin_dirs")
        if isinstance(plugin_dirs, list):
            for item in plugin_dirs:
                if isinstance(item, str) and item.strip():
                    cmd += ["--plugin-dir", item.strip()]
        return cmd

    @staticmethod
    def _build_prompt(request: DelegationRun) -> str:
        return f"Task objective:\n{request.objective}"
