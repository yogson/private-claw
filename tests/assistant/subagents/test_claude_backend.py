"""Tests for Claude Code backend adapter."""

import asyncio

import pytest

from assistant.subagents.backends.claude_code import ClaudeCodeBackendAdapter
from assistant.subagents.contracts import DelegationStageRun


class _Proc:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()


class _SlowProc(_Proc):
    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(5)
        return await super().communicate()


@pytest.mark.asyncio
async def test_execute_stage_parses_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(returncode=0, stdout='{"result":"done","usage":{"input":1,"output":2}}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    stage = DelegationStageRun(
        task_id="t1",
        stage_id="implement",
        purpose="implement",
        model_id="claude-sonnet-4-5",
        objective="Implement feature",
    )
    result = await adapter.execute_stage(stage)
    assert result.ok is True
    assert result.output_text == "done"
    assert result.usage == {"input": 1, "output": 2}


@pytest.mark.asyncio
async def test_execute_stage_handles_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    stage = DelegationStageRun(
        task_id="t1",
        stage_id="review",
        purpose="review",
        model_id="claude-sonnet-4-5",
        objective="Review feature",
    )
    result = await adapter.execute_stage(stage)
    assert result.ok is False
    assert result.error == "boom"


@pytest.mark.asyncio
async def test_execute_stage_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _SlowProc:
        return _SlowProc(returncode=0, stdout="ok")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    stage = DelegationStageRun(
        task_id="t1",
        stage_id="implement",
        purpose="implement",
        model_id="claude-sonnet-4-5",
        objective="Implement feature",
        timeout_seconds=1,
    )
    result = await adapter.execute_stage(stage)
    assert result.ok is False
    assert "timed out" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_stage_handles_missing_cli_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    stage = DelegationStageRun(
        task_id="t1",
        stage_id="review",
        purpose="review",
        model_id="claude-sonnet-4-5",
        objective="Review feature",
    )
    result = await adapter.execute_stage(stage)
    assert result.ok is False
    assert "not found" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_stage_falls_back_to_plain_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(returncode=0, stdout="plain text output")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    stage = DelegationStageRun(
        task_id="t1",
        stage_id="review",
        purpose="review",
        model_id="claude-sonnet-4-5",
        objective="Review feature",
    )
    result = await adapter.execute_stage(stage)
    assert result.ok is True
    assert result.output_text == "plain text output"


def test_build_command_includes_backend_params() -> None:
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    stage = DelegationStageRun(
        task_id="t1",
        stage_id="review",
        purpose="review",
        model_id="claude-sonnet-4-5",
        objective="Review feature",
        backend_params={
            "effort": "high",
            "permission_mode": "plan",
            "add_dirs": ["src", "tests"],
        },
    )
    command = adapter._build_command(stage, "prompt")
    assert "--effort" in command
    assert "high" in command
    assert "--permission-mode" in command
    assert "plan" in command
    assert command.count("--add-dir") == 2
