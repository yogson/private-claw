"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Claude Code streaming backend adapter using claude-agent-sdk.

Provides the same execution interface as ClaudeCodeBackendAdapter but uses the
official Anthropic claude-agent-sdk for streaming output and supports the
AskUserQuestion feedback loop via a configurable question relay callback.
"""

import asyncio
import contextlib
import dataclasses
import os
import shutil
import sys
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any, TypeGuard, cast, get_args

import structlog

from assistant.subagents.coordinator import DELEGATION_RELAY_TIMEOUT_S

# Extend the SDK's stdin-close timeout so it stays open long enough for a
# human to respond to an AskUserQuestion relay.  We add 10 s of headroom on
# top of the coordinator's relay timeout (DELEGATION_RELAY_TIMEOUT_S).
# The SDK reads this env var in Query.__init__ (parent process), so setting it
# here at module import time ensures it takes effect before query() is called.
os.environ.setdefault(
    "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT",
    str(DELEGATION_RELAY_TIMEOUT_S * 1000 + 10_000),
)

from assistant.core.config.schemas import SubagentPermissionMode
from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.interfaces import DelegationBackendAdapterInterface
from assistant.subagents.permission_modes import (
    PermissionModeMonitor,
    explain_cli_rejection,
    resolve_permission_mode,
)

logger = structlog.get_logger(__name__)

# Import SDK at module level so it can be patched in tests.
# Import failures are caught at execution time in execute().
try:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        PermissionMode,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        SystemMessage,
        TaskNotificationMessage,
        TaskStartedMessage,
        ToolPermissionContext,
        query,
    )

    from assistant.subagents.backends.log_formatting import write_log_lines
    from assistant.subagents.backends.sdk_transport import QuietCloseTransport

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


# Tail of the CLI's stderr kept per run, to explain a rejected flag.
_STDERR_TAIL_LINES = 20


def _is_sdk_permission_mode(mode: str) -> "TypeGuard[PermissionMode]":
    # The SDK's typed PermissionMode lags behind the CLI (0.1.50 lacks "auto"
    # and "dontAsk"); modes it does not know go through extra_args instead.
    return mode in get_args(PermissionMode)


def _normalize_answer(reply: str, labels: list[str]) -> str:
    """Map a free-text reply to an option label when it names one (by number or text)."""
    if reply.isdigit() and 1 <= int(reply) <= len(labels):
        return labels[int(reply) - 1]
    for label in labels:
        if label and reply.casefold() == label.casefold():
            return label
    return reply


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

    def __init__(
        self,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        cli_path: str | None = None,
    ) -> None:
        # Keyed by task_id; each entry is an async callable that receives
        # (question, options) and returns the user's answer as a string.
        self._task_relays: dict[str, Callable[[str, list[str]], Awaitable[str]]] = {}
        # MCP servers exposed to every sub-agent run, passed to the CLI as
        # --mcp-config.  Sub-agents run with --setting-sources "" (SDK default),
        # so servers registered in ~/.claude.json or settings.json never reach them.
        self._mcp_servers = mcp_servers or {}
        # The SDK prefers its bundled CLI over PATH, and the bundled copy lags
        # behind: old CLIs send thinking={type:"enabled", budget_tokens}, which
        # newer models (e.g. claude-opus-5-5) reject with a 400.  Use the
        # system-installed claude (same binary as the claude_code backend) and
        # fall back to the bundled one only when none is on PATH.
        self._cli_path = cli_path or shutil.which("claude")

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
        permission_mode = resolve_permission_mode(
            request.backend_params.get("permission_mode"),
            source="claude_code_streaming",
            task_id=request.task_id,
        )
        stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

        def _on_stderr(line: str) -> None:
            # Piping stderr (needed to read it) stops the CLI inheriting ours;
            # forward each line so it still reaches the service log.
            stderr_tail.append(line)
            print(line, file=sys.stderr)

        async def _answer_ask_user_question(
            input_data: dict[str, Any],
        ) -> "PermissionResultAllow | PermissionResultDeny":
            # The CLI's AskUserQuestion input is {"questions": [{"question",
            # "header", "options": [{"label", "description"}], "multiSelect"}]}
            # and it expects {"questions": ..., "answers": {question: answer}}
            # back; any other shape makes it report "The user did not answer".
            questions = input_data.get("questions")
            if relay is None or not isinstance(questions, list) or not questions:
                logger.warning(
                    "ask_user_question_unanswerable",
                    task_id=request.task_id,
                    has_relay=relay is not None,
                )
                return PermissionResultDeny(
                    message=(
                        "No channel to ask the user is available. Do not ask again: "
                        "choose the most reasonable option yourself and state the assumption."
                    )
                )
            answers: dict[str, str] = {}
            for item in questions:
                if not isinstance(item, dict):
                    continue
                question = str(item.get("question", ""))
                raw_options = item.get("options")
                options = [o for o in raw_options if isinstance(o, dict)] if raw_options else []
                labels = [str(o.get("label", "")) for o in options]
                # Buttons carry the bare labels (the answer text comes back as the
                # label); descriptions go into the message body.
                notes = [
                    f"• {lbl} — {o['description']}"
                    for o, lbl in zip(options, labels, strict=True)
                    if o.get("description")
                ]
                text = "\n".join([question, *notes]) if notes else question
                # Relay owns the timeout; the coordinator's _relay wraps the
                # future wait with asyncio.wait_for.
                reply = (await relay(text, labels)).strip()
                answers[question] = _normalize_answer(reply, labels)
            return PermissionResultAllow(updated_input={**input_data, "answers": answers})

        async def _can_use_tool(
            tool_name: str,
            input_data: dict[str, Any],
            context: "ToolPermissionContext",
        ) -> "PermissionResultAllow | PermissionResultDeny":
            if tool_name == "AskUserQuestion":
                return await _answer_ask_user_question(input_data)
            if permission_mode == "bypassPermissions":
                # Explicitly requested: the few prompts bypass mode still
                # raises (e.g. critical-path removals) are approved too.
                return PermissionResultAllow()
            # Anything else reaching us is a prompt the permission mode did not
            # resolve (auto-mode classifier fallback after repeated blocks, ask
            # rules, Manual-mode prompts). Approving it here would silently
            # turn the run into bypassPermissions, so deny it.
            logger.warning(
                "subagent.permission_prompt.denied",
                task_id=request.task_id,
                tool_name=tool_name,
                permission_mode=permission_mode,
            )
            return PermissionResultDeny(
                message=(
                    f"{tool_name} needs approval, and nobody can approve it in this delegated "
                    f"run (permission mode: {permission_mode}). Do not retry it: use another "
                    "approach, or finish and report which action needs the user's approval."
                )
            )

        sdk_options = self._build_options(request, _can_use_tool, permission_mode, _on_stderr)
        prompt = request.objective

        try:
            result_msg: ResultMessage | None = None
            # Only the final ResultMessage carries the answer: earlier ones are
            # interim turns (e.g. "waiting for the background watch").
            final_text = ""
            # Background tasks (run_in_background / Monitor) started but not yet
            # reported finished via a task_notification.
            pending_bg: set[str] = set()
            mode_monitor = PermissionModeMonitor(permission_mode, request.task_id)
            mode_error: str | None = None

            # Set once the run is genuinely over (a ResultMessage with no
            # background tasks pending).  Until then the prompt iterator stays
            # open, which keeps the CLI's stdin open: the SDK closes stdin as
            # soon as the prompt iterator is exhausted and the first result has
            # arrived, and after that every can_use_tool / hook control request
            # the CLI sends in a later turn (e.g. the turn it starts when a
            # background task finishes) fails with "Stream closed", so the tool
            # call is rejected or the turn aborted.
            run_finished = asyncio.Event()

            # can_use_tool requires an AsyncIterable prompt (SDK constraint).
            async def _prompt_iter() -> AsyncGenerator[dict[str, Any], None]:
                yield {
                    "type": "user",
                    "session_id": "",
                    "message": {"role": "user", "content": f"Task objective:\n{prompt}"},
                    "parent_tool_use_id": None,
                }
                await run_finished.wait()

            log_path = Path(request.log_path) if request.log_path else None

            # Set when the run must stop before it does anything (e.g. the CLI
            # did not enter the requested permission mode).
            aborted = asyncio.Event()

            async def _handle_message(msg: Any) -> None:
                nonlocal result_msg, final_text, mode_error
                if aborted.is_set():
                    return  # draining until the cancellation lands
                if log_path is not None:
                    await write_log_lines(log_path, msg, request.task_id)
                is_system = isinstance(msg, SystemMessage)
                mode_error = mode_monitor.observe(is_system, msg.data if is_system else {})
                if mode_error is not None:
                    # Never let the run continue in a mode nobody asked for
                    # (e.g. Manual instead of an unavailable auto mode).
                    run_finished.set()
                    aborted.set()
                elif isinstance(msg, TaskStartedMessage):
                    pending_bg.add(msg.task_id)
                elif isinstance(msg, TaskNotificationMessage):
                    pending_bg.discard(msg.task_id)
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
                    final_text = msg.result or ""
                    if msg.is_error or not pending_bg:
                        run_finished.set()

            async def _run_query() -> None:
                prompt_iter = _prompt_iter()
                # query() only builds the transport itself, with this same
                # option tweak for can_use_tool, when none is passed in.
                transport = QuietCloseTransport(
                    prompt=prompt_iter,
                    options=dataclasses.replace(sdk_options, permission_prompt_tool_name="stdio"),
                )
                async for msg in query(
                    prompt=prompt_iter, options=sdk_options, transport=transport
                ):
                    await _handle_message(msg)

            task = asyncio.create_task(_run_query())
            abort_wait = asyncio.create_task(aborted.wait())
            try:
                try:
                    done, _ = await asyncio.wait(
                        {task, abort_wait},
                        timeout=request.timeout_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if task in done:
                        task.result()  # re-raise a failure of the run itself
                    elif not done:
                        raise TimeoutError
                except TimeoutError:
                    return DelegationResult(ok=False, error="claude-agent-sdk run timed out")
            finally:
                abort_wait.cancel()
                # Whatever path got us out of the wait (timeout, abort, outer
                # cancellation, unexpected exception), the inner _run_query task
                # must be terminated and awaited. Otherwise the claude-agent-sdk
                # subprocess orphans and keeps consuming memory until OOM.
                # Cancelling the task (rather than breaking out of the stream)
                # lands inside the SDK's generators, so they close the CLI in
                # this task; an abandoned generator would be finalized in
                # another task, where anyio's cancel scope refuses to exit.
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

        except Exception as exc:
            rejection = explain_cli_rejection(permission_mode, "\n".join(stderr_tail))
            return DelegationResult(
                ok=False, error=rejection or f"claude-agent-sdk execution failed: {exc}"
            )

        if mode_error is not None:
            return DelegationResult(ok=False, error=mode_error)

        if result_msg is None:
            return DelegationResult(
                ok=False,
                error=(
                    "Sub-agent stream ended without a ResultMessage"
                    " (max_turns exhaustion or SDK error)"
                ),
            )

        if result_msg.is_error:
            return DelegationResult(
                ok=False,
                error=result_msg.result or "claude-agent-sdk returned an error",
                usage=result_msg.usage or {},
            )

        output_text = final_text.strip()
        usage: dict[str, Any] = result_msg.usage or {}

        if pending_bg:
            # The stream ended (closing the CLI) while background tasks were
            # still running, so the last text is an interim "waiting" note.
            logger.warning(
                "subagent_ended_with_pending_background_tasks",
                task_id=request.task_id,
                pending=sorted(pending_bg),
            )
            return DelegationResult(
                ok=False,
                error=(
                    f"Sub-agent finished while {len(pending_bg)} background task(s) were "
                    "still running; its last message was interim, not a final answer: "
                    f"{output_text[:500]}"
                ),
                usage=usage,
            )

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
        permission_mode: SubagentPermissionMode,
        on_stderr: Callable[[str], None] | None = None,
    ) -> Any:
        params = request.backend_params
        cwd = params.get("directory") or None

        raw_effort = str(params.get("effort", "")).strip() or None
        effort = raw_effort if raw_effort in ("low", "medium", "high", "max") else None
        # The SDK's typed effort field predates "xhigh", but the CLI accepts it;
        # route it through extra_args, which the SDK turns into `--effort xhigh`.
        extra_args: dict[str, str | None] = {"effort": "xhigh"} if raw_effort == "xhigh" else {}
        if raw_effort is not None and effort is None and not extra_args:
            logger.warning("claude_streaming.effort_ignored", effort=raw_effort)

        # Same for permission modes the SDK's typed field does not know yet
        # ("auto", "dontAsk"): `--permission-mode auto` via extra_args, with the
        # typed field left unset so the flag is not passed twice.
        sdk_permission_mode: PermissionMode | None = None
        if _is_sdk_permission_mode(permission_mode):
            sdk_permission_mode = permission_mode
        else:
            extra_args["permission-mode"] = permission_mode

        add_dirs_raw = params.get("add_dirs")
        add_dirs: list[str] = (
            [str(d) for d in add_dirs_raw if isinstance(d, str) and d.strip()]
            if isinstance(add_dirs_raw, list)
            else []
        )

        plugin_dirs_raw = params.get("plugin_dirs")
        plugins: list[dict[str, str]] = (
            [
                {"type": "local", "path": str(d)}
                for d in plugin_dirs_raw
                if isinstance(d, str) and d.strip()
            ]
            if isinstance(plugin_dirs_raw, list)
            else []
        )

        return ClaudeAgentOptions(
            model=request.model_id,
            cli_path=self._cli_path,
            mcp_servers=cast(Any, dict(self._mcp_servers)),
            max_turns=request.max_turns,
            cwd=cwd,
            effort=cast(Any, effort),
            extra_args=extra_args,
            permission_mode=sdk_permission_mode,
            add_dirs=cast(Any, add_dirs),
            plugins=cast(Any, plugins),
            can_use_tool=can_use_tool,
            stderr=on_stderr,
            # Stub hook keeps SDK stdin open for ask_question relay (Fix 3).
            # Even if the SDK patch (Fix 1) is lost on reinstall, bool(hooks)
            # remains truthy so wait_for_result_and_end_input() won't close
            # stdin early. The empty matcher list is harmless.
            hooks={"PreToolUse": []},
        )
