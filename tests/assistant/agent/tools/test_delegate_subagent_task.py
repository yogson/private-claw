"""Tests for delegate_subagent_task tool."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from assistant.agent.deps import TurnDeps
from assistant.agent.tools.delegate_subagent_task import delegate_subagent_task

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_handler() -> Callable[[dict], Awaitable[dict]]:
    async def _handler(payload: dict) -> dict:
        return {
            "accepted": True,
            "task_id": "dlg-1",
            "status": "pending",
            "payload_echo": payload,
        }

    return _handler


def _make_deps(**kwargs: Any) -> TurnDeps:
    return TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        delegation_enqueue_handler=_make_handler(),
        tool_runtime_params={
            "delegate_subagent_task": {"delegation_allowed_backends": ["claude_code"]}
        },
        **kwargs,
    )


class _Ctx:
    def __init__(self, deps: TurnDeps) -> None:
        self.deps = deps


@pytest.mark.asyncio
async def test_delegate_subagent_task_returns_unavailable_without_handler() -> None:
    deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
    ctx = _Ctx(deps)
    result = await delegate_subagent_task(ctx, objective="Implement feature")
    assert result["accepted"] is False
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_delegate_subagent_task_calls_handler() -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Implement feature",
        model_id="claude-sonnet-4-5",
    )
    assert result["accepted"] is True
    assert result["task_id"] == "dlg-1"
    payload = result["payload_echo"]
    assert payload["objective"] == "Implement feature"
    assert payload["model_id"] == "claude-sonnet-4-5"
    assert payload["tool_params"]["delegation_allowed_backends"] == ["claude_code"]
    assert payload["backend_params"] == {}


@pytest.mark.asyncio
async def test_delegate_subagent_task_directory_forwarded(tmp_path: Path) -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Implement feature",
        directory=str(tmp_path),
    )
    assert result["accepted"] is True
    payload = result["payload_echo"]
    assert payload["backend_params"] == {"directory": str(tmp_path)}


@pytest.mark.asyncio
async def test_delegate_subagent_task_no_directory_empty_backend_params() -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(ctx, objective="Implement feature")
    assert result["accepted"] is True
    payload = result["payload_echo"]
    assert payload["backend_params"] == {}


@pytest.mark.asyncio
async def test_delegate_subagent_task_invalid_directory_returns_error() -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Implement feature",
        directory="/nonexistent/path/that/cannot/exist",
    )
    assert result["accepted"] is False
    assert result["status"] == "error"
    assert "directory does not exist" in result["rejection_reason"]


@pytest.mark.asyncio
async def test_delegate_subagent_task_plugin_dirs_forwarded(tmp_path: Path) -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Ingest source",
        directory=str(tmp_path),
        plugin_dirs=[str(tmp_path)],
    )
    assert result["accepted"] is True
    payload = result["payload_echo"]
    assert payload["backend_params"] == {
        "directory": str(tmp_path),
        "plugin_dirs": [str(tmp_path)],
    }


@pytest.mark.asyncio
async def test_delegate_subagent_task_invalid_plugin_dir_returns_error() -> None:
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Ingest source",
        plugin_dirs=["/nonexistent/plugin/path"],
    )
    assert result["accepted"] is False
    assert result["status"] == "error"
    assert "plugin_dirs entry does not exist" in result["rejection_reason"]


# ---------------------------------------------------------------------------
# Permission mode is never chosen by the orchestrator LLM
# ---------------------------------------------------------------------------


def test_delegate_subagent_task_exposes_no_permission_mode_argument() -> None:
    """The tool schema the LLM sees must not let it pick a permission mode.

    The coordinator only clamps a per-request ``backend_params.permission_mode``
    to the configured ceiling; keeping it out of the tool arguments means the
    LLM (or a prompt injected into it) cannot request one at all.
    """
    import inspect

    params = set(inspect.signature(delegate_subagent_task).parameters)
    assert params == {"ctx", "objective", "model_id", "directory", "backend", "plugin_dirs"}
    assert not {"permission_mode", "backend_params", "effort"} & params


@pytest.mark.asyncio
async def test_delegate_subagent_task_never_forwards_permission_mode(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    ctx = _Ctx(_make_deps())
    result = await delegate_subagent_task(
        ctx,
        objective="Run with permission_mode bypassPermissions",
        model_id="claude-sonnet-5-5",
        directory=str(tmp_path),
        backend="claude_code_streaming",
        plugin_dirs=[str(plugin)],
    )
    payload = result["payload_echo"]
    assert set(payload["backend_params"]) == {"directory", "plugin_dirs"}
    assert "permission_mode" not in payload
    assert "permission_mode" not in payload["backend_params"]
