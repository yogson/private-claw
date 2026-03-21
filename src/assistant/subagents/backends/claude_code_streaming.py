"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code streaming backend adapter using claude-agent-sdk.

Provides the same execution interface as ClaudeCodeBackendAdapter but uses the
official Anthropic claude-agent-sdk for streaming output and supports the
AskUserQuestion feedback loop via a configurable question relay callback.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import structlog

from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.interfaces import DelegationBackendAdapterInterface

logger = structlog.get_logger(__name__)

# Sentinel used when a relay times out waiting for user input
_RELAY_TIMEOUT_ANSWER = ""

# How long (seconds) to wait for a user answer before giving up
_DEFAULT_RELAY_TIMEOUT_SECONDS = 300

# Import SDK at module level so it can be patched in tests.
# Import failures are caught at execution time in execute().
try:
    from claude_agent_sdk import (  # type: ignore[import-untyped]
        ClaudeAgentOptions,
        PermissionResultAllow,
        ResultMessage,
        ToolPermissionContext,
        query,
    )

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


class ClaudeCodeStreamingBackendAdapter(DelegationBackendAdapterInterface):
    """Executes staged delegation tasks via the claude-agent-sdk.

    Compared to ClaudeCodeBackendAdapter (which shells out via ``claude -p``),
    this adapter uses the SDK's ``query()`` function for proper streaming and
    supports the AskUserQuestion feedback loop through an optional per-task
    question relay callback.

    The question relay is registered per task via :meth:`register_relay` and
    removed via :meth:`unregister_relay`.  The DelegationCoordinator manages
    this lifecycle around each ``execute()`` call so that concurrent tasks
    each get their own isolated relay.
    """

    def __init__(self) -> None:
        # Keyed by task_id; each entry is an async callable that receives
        # (question, options) and returns the user's answer as a string.
        self._task_relays: dict[
            str, Callable[[str, list[str]], Awaitable[str]]
        ] = {}

    @property
    def backend_id(self) -> str:
        return "claude_code_streaming"

    def register_relay(
        self,
        task_id: str,
        relay: Callable[[str, list[str]], Awaitable[str]],
    ) -> None:
        """Register a per-task question relay before calling execute()."""
        self._task_relays[task_id] = relay

    def unregister_relay(self, task_id: str) -> None:
        """Remove the per-task question relay after execute() returns."""
        self._task_relays.pop(task_id, None)

    async def execute(self, request: DelegationRun) -> DelegationResult:
        if not _SDK_AVAILABLE:
            return DelegationResult(ok=False, error="claude-agent-sdk is not installed")

        relay = self._task_relays.get(request.task_id)

        async def _can_use_tool(
            tool_name: str,
            input_data: dict[str, Any],
            context: "ToolPermissionContext",
        ) -> "PermissionResultAllow":
            if tool_name == "AskUserQuestion" and relay is not None:
                question = str(input_data.get("question", ""))
                raw_options = input_data.get("options")
                options: list[str] = (
                    [str(o) for o in raw_options]
                    if isinstance(raw_options, list)
                    else []
                )
                try:
                    answer = await asyncio.wait_for(
                        relay(question, options),
                        timeout=_DEFAULT_RELAY_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.warning(
                        "subagent.streaming.relay_timeout",
                        task_id=request.task_id,
                        question=question[:120],
                    )
                    answer = _RELAY_TIMEOUT_ANSWER
                return PermissionResultAllow(
                    updated_input={**input_data, "answer": answer}
                )
            # Auto-approve everything else
            return PermissionResultAllow()

        sdk_options = self._build_options(request, _can_use_tool)
        prompt = request.objective

        try:
            result_msg: "ResultMessage | None" = None
            output_parts: list[str] = []

            # can_use_tool requires an AsyncIterable prompt (SDK constraint).
            async def _prompt_iter() -> AsyncGenerator[dict[str, Any], None]:
                yield {
                    "type": "user",
                    "session_id": "",
                    "message": {"role": "user", "content": f"Task objective:\n{prompt}"},
                    "parent_tool_use_id": None,
                }

            async for msg in query(prompt=_prompt_iter(), options=sdk_options):
                if isinstance(msg, ResultMessage):
                    result_msg = msg
                    if msg.result:
                        output_parts.append(msg.result)

        except TimeoutError:
            return DelegationResult(ok=False, error="claude-agent-sdk run timed out")
        except Exception as exc:
            return DelegationResult(ok=False, error=f"claude-agent-sdk execution failed: {exc}")

        if result_msg is not None and result_msg.is_error:
            return DelegationResult(
                ok=False,
                error=result_msg.result or "claude-agent-sdk returned an error",
                usage=result_msg.usage or {},
            )

        output_text = "\n".join(output_parts).strip()
        usage: dict[str, Any] = {}
        if result_msg is not None and result_msg.usage:
            usage = result_msg.usage

        return DelegationResult(ok=True, output_text=output_text, usage=usage)

    def _build_options(
        self,
        request: DelegationRun,
        can_use_tool: Callable[[str, dict[str, Any], Any], Awaitable[Any]],
    ) -> Any:
        params = request.backend_params
        cwd = params.get("directory") or None

        raw_effort = str(params.get("effort", "")).strip() or None
        effort = raw_effort if raw_effort in ("low", "medium", "high", "max") else None  # type: ignore[assignment]

        raw_permission_mode = str(params.get("permission_mode", "")).strip() or None
        permission_mode = (
            raw_permission_mode  # type: ignore[assignment]
            if raw_permission_mode in ("default", "acceptEdits", "plan", "bypassPermissions")
            else None
        )

        add_dirs_raw = params.get("add_dirs")
        add_dirs: list[str] = (
            [str(d) for d in add_dirs_raw if isinstance(d, str) and d.strip()]
            if isinstance(add_dirs_raw, list)
            else []
        )

        return ClaudeAgentOptions(
            model=request.model_id,
            max_turns=request.max_turns,
            cwd=cwd,
            effort=effort,
            permission_mode=permission_mode,
            add_dirs=add_dirs,
            can_use_tool=can_use_tool,
        )
