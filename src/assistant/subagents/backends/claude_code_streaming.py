"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code streaming backend adapter using claude-agent-sdk.

Provides the same execution interface as ClaudeCodeBackendAdapter but uses the
official Anthropic claude-agent-sdk for streaming output and supports the
AskUserQuestion feedback loop via a configurable question relay callback.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import structlog

# Extend the SDK's stdin-close timeout to 310 s so it stays open long enough
# for a human to respond to an AskUserQuestion relay (relay timeout is 300 s).
# The SDK reads this env var in Query.__init__ (parent process), so setting it
# here at module import time ensures it takes effect before query() is called.
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "310000")

from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.interfaces import DelegationBackendAdapterInterface

logger = structlog.get_logger(__name__)

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

    @property
    def supports_relay(self) -> bool:
        return True

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
            if tool_name == "AskUserQuestion":
                question = str(input_data.get("question", ""))
                raw_options = input_data.get("options")
                options: list[str] = (
                    [str(o) for o in raw_options]
                    if isinstance(raw_options, list)
                    else []
                )
                if relay is not None:
                    # Relay owns the timeout; the coordinator's _relay wraps the
                    # future wait with asyncio.wait_for.
                    answer = await relay(question, options)
                else:
                    # No relay registered; inject empty answer so the agent
                    # receives a well-formed response instead of a missing field.
                    logger.warning(
                        "ask_user_question_no_relay",
                        task_id=request.task_id,
                        question=question,
                    )
                    answer = ""
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
                    "session_id": request.task_id,
                    "message": {"role": "user", "content": f"Task objective:\n{prompt}"},
                    "parent_tool_use_id": None,
                }

            async def _run_query() -> None:
                nonlocal result_msg
                async for msg in query(prompt=_prompt_iter(), options=sdk_options):
                    if isinstance(msg, ResultMessage):
                        result_msg = msg
                        if msg.result:
                            output_parts.append(msg.result)

            task = asyncio.ensure_future(_run_query())
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=request.timeout_seconds)
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as cleanup_exc:
                    logger.debug("sdk_task_cleanup", exc=str(cleanup_exc))
                return DelegationResult(ok=False, error="claude-agent-sdk run timed out")

        except Exception as exc:
            return DelegationResult(ok=False, error=f"claude-agent-sdk execution failed: {exc}")

        if result_msg is None:
            return DelegationResult(
                ok=False,
                error="Sub-agent stream ended without a ResultMessage (max_turns exhaustion or SDK error)",
            )

        if result_msg.is_error:
            return DelegationResult(
                ok=False,
                error=result_msg.result or "claude-agent-sdk returned an error",
                usage=result_msg.usage or {},
            )

        output_text = "\n".join(output_parts).strip()
        usage: dict[str, Any] = result_msg.usage or {}

        if not output_text:
            logger.warning(
                "subagent_empty_output",
                task_id=request.task_id,
                note="Agent produced no text output; may have only edited files.",
            )

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
            else "bypassPermissions"  # safe default: no TTY to route approval requests to
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
            # Stub hook keeps SDK stdin open for ask_question relay (Fix 3).
            # Even if the SDK patch (Fix 1) is lost on reinstall, bool(hooks)
            # remains truthy so wait_for_result_and_end_input() won't close
            # stdin early. The empty matcher list is harmless.
            hooks={"PreToolUse": []},
        )
