"""Tests for Claude Code backend adapter."""

import asyncio
import json

import pytest

from assistant.subagents.backends.claude_code import ClaudeCodeBackendAdapter
from assistant.subagents.contracts import DelegationRun


class _Proc:
    def __init__(self, returncode: int | None, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode if self.returncode is not None else -9


class _SlowProc(_Proc):
    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(5)
        return await super().communicate()


class _HangingProc(_Proc):
    """Process whose communicate() never resolves on its own - only external
    cancellation (not a timeout) ends the await."""

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


@pytest.mark.asyncio
async def test_execute_parses_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(returncode=0, stdout='{"result":"done","usage":{"input":1,"output":2}}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Implement feature",
        model_id="claude-sonnet-4-5",
    )
    result = await adapter.execute(request)
    assert result.ok is True
    assert result.output_text == "done"
    assert result.usage == {"input": 1, "output": 2}


@pytest.mark.asyncio
async def test_execute_handles_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Review feature",
        model_id="claude-sonnet-4-5",
    )
    result = await adapter.execute(request)
    assert result.ok is False
    assert result.error == "boom"


@pytest.mark.asyncio
async def test_execute_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _SlowProc(returncode=None, stdout="ok")

    async def _fake_create(*_args: object, **_kwargs: object) -> _SlowProc:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Implement feature",
        model_id="claude-sonnet-4-5",
        timeout_seconds=1,
    )
    result = await adapter.execute(request)
    assert result.ok is False
    assert "timed out" in (result.error or "")
    # A timed-out run must not leave the CLI subprocess orphaned.
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_execute_kills_process_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _HangingProc(returncode=None, stdout="ok")

    async def _fake_create(*_args: object, **_kwargs: object) -> _HangingProc:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Implement feature",
        model_id="claude-sonnet-4-5",
        timeout_seconds=60,
    )
    task = asyncio.create_task(adapter.execute(request))
    await asyncio.sleep(0)  # let it reach the hanging communicate() await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_execute_handles_missing_cli_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Review feature",
        model_id="claude-sonnet-4-5",
    )
    result = await adapter.execute(request)
    assert result.ok is False
    assert "not found" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_falls_back_to_plain_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _Proc:
        return _Proc(returncode=0, stdout="plain text output")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Review feature",
        model_id="claude-sonnet-4-5",
    )
    result = await adapter.execute(request)
    assert result.ok is True
    assert result.output_text == "plain text output"


def test_build_command_includes_backend_params() -> None:
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Review feature",
        model_id="claude-sonnet-4-5",
        backend_params={
            "effort": "high",
            "permission_mode": "plan",
            "add_dirs": ["src", "tests"],
            "plugin_dirs": ["/path/to/plugin-a", "/path/to/plugin-b"],
        },
    )
    command = adapter._build_command(request, "prompt")
    assert "--effort" in command
    assert "high" in command
    assert "--permission-mode" in command
    assert "plan" in command
    assert command.count("--add-dir") == 2
    assert command.count("--plugin-dir") == 2
    assert "/path/to/plugin-a" in command
    assert "/path/to/plugin-b" in command


def test_build_command_passes_mcp_servers_as_inline_config() -> None:
    adapter = ClaudeCodeBackendAdapter(
        binary="claude",
        mcp_servers={"ui-skills": {"type": "http", "url": "https://www.ui-skills.com/mcp"}},
    )
    request = DelegationRun(task_id="t1", objective="Build UI", model_id="claude-sonnet-4-5")
    command = adapter._build_command(request, "prompt")
    assert "--mcp-config" in command
    payload = json.loads(command[command.index("--mcp-config") + 1])
    assert payload["mcpServers"]["ui-skills"]["url"] == "https://www.ui-skills.com/mcp"


def test_build_command_omits_mcp_config_when_no_servers() -> None:
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Build UI", model_id="claude-sonnet-4-5")
    assert "--mcp-config" not in adapter._build_command(request, "prompt")
