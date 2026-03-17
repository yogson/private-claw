"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code backend adapter for delegated staged execution.
"""

import asyncio
import json
from typing import Any

from assistant.subagents.contracts import DelegationStageResult, DelegationStageRun
from assistant.subagents.interfaces import DelegationBackendAdapterInterface

_DEFAULT_CLAUDE_BINARY = "claude"


class ClaudeCodeBackendAdapter(DelegationBackendAdapterInterface):
    """Executes staged delegation tasks via local Claude Code CLI."""

    def __init__(self, binary: str = _DEFAULT_CLAUDE_BINARY) -> None:
        self._binary = binary

    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute_stage(self, stage: DelegationStageRun) -> DelegationStageResult:
        prompt = self._build_prompt(stage)
        cmd = self._build_command(stage, prompt)
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=stage.timeout_seconds,
            )
        except TimeoutError:
            return DelegationStageResult(ok=False, error="claude stage timed out")
        except FileNotFoundError:
            return DelegationStageResult(ok=False, error="claude CLI binary not found")
        except Exception as exc:  # pragma: no cover - defensive runtime branch
            return DelegationStageResult(ok=False, error=f"claude execution failed: {exc}")

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            msg = stderr or stdout or f"claude exited with code {process.returncode}"
            return DelegationStageResult(ok=False, error=msg)

        parsed = self._parse_json_output(stdout)
        usage = parsed.get("usage", {}) if isinstance(parsed, dict) else {}
        text = self._extract_text(parsed, stdout)
        artifacts: dict[str, Any] = {"raw_stdout": stdout}
        return DelegationStageResult(ok=True, output_text=text, artifacts=artifacts, usage=usage)

    def _build_command(self, stage: DelegationStageRun, prompt: str) -> list[str]:
        cmd = [
            self._binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            stage.model_id,
            "--max-turns",
            str(stage.max_turns),
        ]
        effort = str(stage.backend_params.get("effort", "")).strip()
        if effort:
            cmd += ["--effort", effort]
        permission_mode = str(stage.backend_params.get("permission_mode", "")).strip()
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]
        add_dirs = stage.backend_params.get("add_dirs")
        if isinstance(add_dirs, list):
            for item in add_dirs:
                if isinstance(item, str) and item.strip():
                    cmd += ["--add-dir", item.strip()]
        return cmd

    @staticmethod
    def _build_prompt(stage: DelegationStageRun) -> str:
        prior = ""
        if stage.prior_stage_outputs:
            joined = "\n\n".join(
                f"- Stage {item.get('stage_id', 'unknown')}: {item.get('output_text', '')}"
                for item in stage.prior_stage_outputs
            )
            prior = f"\n\nPrevious stage outputs:\n{joined}"
        return (
            f"Task objective:\n{stage.objective}\n\n"
            f"Current stage: {stage.stage_id} ({stage.purpose}).{prior}"
        )

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
