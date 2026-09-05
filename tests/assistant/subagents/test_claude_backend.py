"""Tests for Claude Code backend adapter."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from assistant.subagents.backends.claude_code import ClaudeCodeBackendAdapter
from assistant.subagents.contracts import DelegationRun


class _FakeStream:
    """Minimal async-iterable line stream mimicking asyncio.StreamReader."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{line}\n".encode() for line in lines]

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _HangingStream:
    """A stream whose iteration never completes on its own - only external
    cancellation (timeout or coordinator.cancel_task) ends it."""

    def __aiter__(self) -> "_HangingStream":
        return self

    async def __anext__(self) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


class _FakeProcess:
    """Fakes the pieces of asyncio.subprocess.Process that execute() touches:
    stdout/stderr as async line iterables, kill(), and wait()."""

    def __init__(
        self,
        *,
        returncode: int | None,
        stdout_lines: list[str] | None = None,
        stderr_lines: list[str] | None = None,
        hang_stdout: bool = False,
    ) -> None:
        self.returncode = returncode
        self._exited = asyncio.Event()
        if returncode is not None:
            self._exited.set()
        self.stdout: Any = _HangingStream() if hang_stdout else _FakeStream(stdout_lines or [])
        self.stderr: Any = _FakeStream(stderr_lines or [])
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        self.waited = True
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


def _result_line(
    *,
    result: str = "done",
    is_error: bool = False,
    usage: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 100,
            "duration_api_ms": 90,
            "is_error": is_error,
            "num_turns": 1,
            "session_id": "s1",
            "result": result,
            "usage": usage or {},
        }
    )


def _assistant_line(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


@pytest.mark.asyncio
async def test_execute_returns_ok_from_result_line(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=[_result_line(result="done", usage={"input": 1, "output": 2})],
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1", objective="Implement feature", model_id="claude-sonnet-4-5"
    )
    result = await adapter.execute(request)
    assert result.ok is True
    assert result.output_text == "done"
    assert result.usage == {"input": 1, "output": 2}


@pytest.mark.asyncio
async def test_execute_raises_stdio_stream_limit_above_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio's default StreamReader limit is 64KiB per line; a large
    tool_use/tool_result payload on a single --output-format stream-json line
    would otherwise raise LimitOverrunError and get misreported as a failure."""
    proc = _FakeProcess(returncode=0, stdout_lines=[_result_line(result="done")])
    captured_kwargs: dict[str, Any] = {}

    async def _fake_create(*_args: object, **kwargs: object) -> _FakeProcess:
        captured_kwargs.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1", objective="Implement feature", model_id="claude-sonnet-4-5"
    )
    await adapter.execute(request)

    assert captured_kwargs["limit"] > 65536


@pytest.mark.asyncio
async def test_execute_handles_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(returncode=1, stderr_lines=["boom"])

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    result = await adapter.execute(request)
    assert result.ok is False
    assert result.error == "boom"


@pytest.mark.asyncio
async def test_execute_nonzero_exit_prefers_result_line_error_over_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured {"type":"result","is_error":true,"result":"..."} line seen
    before a nonzero exit (e.g. an API error) is more informative than raw
    stderr noise or a bare exit-code message - prefer it."""
    proc = _FakeProcess(
        returncode=1,
        stdout_lines=[_result_line(result="Overloaded: rate limit exceeded", is_error=True)],
        stderr_lines=["some unrelated stderr noise"],
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    result = await adapter.execute(request)
    assert result.ok is False
    assert result.error == "Overloaded: rate limit exceeded"


@pytest.mark.asyncio
async def test_execute_handles_is_error_result_line(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        returncode=0, stdout_lines=[_result_line(result="bad input", is_error=True)]
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    result = await adapter.execute(request)
    assert result.ok is False
    assert result.error == "bad input"


@pytest.mark.asyncio
async def test_execute_no_result_line_falls_back_to_raw_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the process exits 0 but never printed a {"type":"result",...} line
    (e.g. an older/incompatible claude CLI not honoring --output-format
    stream-json), fall back to whatever raw text it did print rather than
    reporting an otherwise-successful run as failed."""
    proc = _FakeProcess(returncode=0, stdout_lines=[_assistant_line("hi")])

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    result = await adapter.execute(request)
    assert result.ok is True
    assert "hi" in result.output_text


@pytest.mark.asyncio
async def test_execute_no_result_line_and_no_output_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProcess(returncode=0, stdout_lines=[])

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    result = await adapter.execute(request)
    assert result.ok is False
    assert "result line" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_skips_malformed_json_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=["not json at all", "", _result_line(result="done")],
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    result = await adapter.execute(request)
    assert result.ok is True
    assert result.output_text == "done"


@pytest.mark.asyncio
async def test_execute_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(returncode=None, hang_stdout=True)

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
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


@pytest.mark.asyncio
async def test_execute_kills_process_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(returncode=None, hang_stdout=True)

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
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
    await asyncio.sleep(0)  # let it reach the hanging stdout read
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed is True


@pytest.mark.asyncio
async def test_execute_handles_missing_cli_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    result = await adapter.execute(request)
    assert result.ok is False
    assert "not found" in (result.error or "")


# ---------------------------------------------------------------------------
# Activity log writing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_writes_log_lines_when_log_path_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "dlg-1.log"
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=[_assistant_line("working on it"), _result_line(result="done")],
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1",
        objective="Implement feature",
        model_id="claude-sonnet-4-5",
        log_path=str(log_path),
    )
    result = await adapter.execute(request)

    assert result.ok is True
    content = log_path.read_text()
    assert "[assistant] working on it" in content
    assert "[result] ok" in content


@pytest.mark.asyncio
async def test_execute_without_log_path_writes_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proc = _FakeProcess(returncode=0, stdout_lines=[_result_line(result="done")])

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(
        task_id="t1", objective="Implement feature", model_id="claude-sonnet-4-5"
    )
    result = await adapter.execute(request)

    assert result.ok is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_execute_without_sdk_still_returns_result_but_skips_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Core result parsing must not depend on claude-agent-sdk being installed -
    only the human-readable log formatting is a soft dependency on it."""
    log_path = tmp_path / "dlg-1.log"
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=[_assistant_line("working on it"), _result_line(result="done")],
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    with patch(
        "assistant.subagents.backends.claude_code._LOG_PARSING_AVAILABLE",
        False,
    ):
        adapter = ClaudeCodeBackendAdapter(binary="claude")
        request = DelegationRun(
            task_id="t1",
            objective="Implement feature",
            model_id="claude-sonnet-4-5",
            log_path=str(log_path),
        )
        result = await adapter.execute(request)

    assert result.ok is True
    assert result.output_text == "done"
    assert not log_path.exists()


@pytest.mark.asyncio
async def test_execute_survives_log_formatting_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bug in log formatting is observability breakage, not a run failure."""
    log_path = tmp_path / "dlg-1.log"
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=[_assistant_line("working on it"), _result_line(result="done")],
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    with patch(
        "assistant.subagents.backends.log_formatting.format_log_lines",
        side_effect=RuntimeError("boom"),
    ):
        adapter = ClaudeCodeBackendAdapter(binary="claude")
        request = DelegationRun(
            task_id="t1",
            objective="Implement feature",
            model_id="claude-sonnet-4-5",
            log_path=str(log_path),
        )
        result = await adapter.execute(request)

    assert result.ok is True
    assert result.output_text == "done"


@pytest.mark.asyncio
async def test_execute_survives_message_parse_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """parse_message reaches into claude-agent-sdk's private _internal module -
    an API-incompatible change there could raise something other than its
    documented MessageParseError (e.g. AttributeError/TypeError). That must
    still only disable logging, never fail the delegated run itself."""
    log_path = tmp_path / "dlg-1.log"
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=[_assistant_line("working on it"), _result_line(result="done")],
    )

    async def _fake_create(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create)
    with patch(
        "assistant.subagents.backends.claude_code.parse_message",
        side_effect=AttributeError("SDK internals changed"),
    ):
        adapter = ClaudeCodeBackendAdapter(binary="claude")
        request = DelegationRun(
            task_id="t1",
            objective="Implement feature",
            model_id="claude-sonnet-4-5",
            log_path=str(log_path),
        )
        result = await adapter.execute(request)

    assert result.ok is True
    assert result.output_text == "done"
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_build_command_includes_stream_json_flags() -> None:
    adapter = ClaudeCodeBackendAdapter(binary="claude")
    request = DelegationRun(task_id="t1", objective="Review feature", model_id="claude-sonnet-4-5")
    command = adapter._build_command(request, "prompt")
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command


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
