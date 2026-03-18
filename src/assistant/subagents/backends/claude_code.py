"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code backend adapter for delegated staged execution.
"""

import asyncio
import json
from typing import Any

from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.interfaces import DelegationBackendAdapterInterface

_DEFAULT_CLAUDE_BINARY = "claude"


class ClaudeCodeBackendAdapter(DelegationBackendAdapterInterface):
    """Executes staged delegation tasks via local Claude Code CLI."""

    def __init__(self, binary: str = _DEFAULT_CLAUDE_BINARY) -> None:
        self._binary = binary

    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute(self, request: DelegationRun) -> DelegationResult:
        prompt = self._build_prompt(request)
        cmd = self._build_command(request, prompt)
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            return DelegationResult(ok=False, error="claude run timed out")
        except FileNotFoundError:
            return DelegationResult(ok=False, error="claude CLI binary not found")
        except Exception as exc:  # pragma: no cover - defensive runtime branch
            return DelegationResult(ok=False, error=f"claude execution failed: {exc}")

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            msg = stderr or stdout or f"claude exited with code {process.returncode}"
            return DelegationResult(ok=False, error=msg)

        parsed = self._parse_json_output(stdout)
        usage = parsed.get("usage", {}) if isinstance(parsed, dict) else {}
        text = self._extract_text(parsed, stdout)
        artifacts: dict[str, Any] = {"raw_stdout": stdout}
        return DelegationResult(ok=True, output_text=text, artifacts=artifacts, usage=usage)

    def _build_command(self, request: DelegationRun, prompt: str) -> list[str]:
        cmd = [
            self._binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            request.model_id,
            "--max-turns",
            str(request.max_turns),
        ]
        effort = str(request.backend_params.get("effort", "")).strip()
        if effort:
            cmd += ["--effort", effort]
        permission_mode = str(request.backend_params.get("permission_mode", "")).strip()
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]
        add_dirs = request.backend_params.get("add_dirs")
        if isinstance(add_dirs, list):
            for item in add_dirs:
                if isinstance(item, str) and item.strip():
                    cmd += ["--add-dir", item.strip()]
        return cmd

    @staticmethod
    def _build_prompt(request: DelegationRun) -> str:
        return f"Task objective:\n{request.objective}"

    @staticmethod
    def _parse_json_output(output: str) -> dict[str, Any]:
        if not output:
            return {}
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                return parsed
            return {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _extract_text(parsed: dict[str, Any], fallback: str) -> str:
        if not parsed:
            return fallback
        text = parsed.get("result")
        if isinstance(text, str) and text.strip():
            return text.strip()
        message = parsed.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return fallback
