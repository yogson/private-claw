"""Tests for launcher process handling: PID files, ownership checks, spawn and stop."""

import signal
import subprocess
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from assistant.launcher.instances import InstanceSpec, parse_instances
from assistant.launcher.process import (
    InstanceState,
    instance_state,
    is_instance_command,
    read_pid,
    spawn,
    stop_pid,
    uvicorn_command,
    uvicorn_log_path,
    wait_healthy,
)

_CMD = "/repo/.venv/bin/python -m uvicorn assistant.api.main:app --host 127.0.0.1 --port 8001"


def _spec(root: Path) -> InstanceSpec:
    [spec] = parse_instances({"instances": {"a": {"port": 8001, "token_env": "TG_A"}}}, root)
    return spec


def _write_pid(spec: InstanceSpec, content: str) -> None:
    spec.pid_file.parent.mkdir(parents=True, exist_ok=True)
    spec.pid_file.write_text(content)


def test_is_instance_command_requires_app_and_exact_port() -> None:
    assert is_instance_command(_CMD, 8001)
    assert not is_instance_command(_CMD, 800)
    assert not is_instance_command(_CMD.replace("--port 8001", "--port 80011"), 8001)
    assert not is_instance_command("/usr/bin/python other.py --port 8001", 8001)
    assert not is_instance_command("uvicorn other:app --port 8001", 8001)


def test_read_pid_handles_missing_and_malformed(tmp_path: Path) -> None:
    assert read_pid(tmp_path / "missing.pid") is None
    (tmp_path / "bad.pid").write_text("not-a-pid")
    assert read_pid(tmp_path / "bad.pid") is None
    (tmp_path / "ok.pid").write_text("123\n")
    assert read_pid(tmp_path / "ok.pid") == 123


def test_instance_state_stopped_running_stale(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    assert instance_state(spec, lambda pid: _CMD) == (InstanceState.STOPPED, None)

    _write_pid(spec, "4242")
    assert instance_state(spec, lambda pid: _CMD) == (InstanceState.RUNNING, 4242)
    # Process gone.
    assert instance_state(spec, lambda pid: None) == (InstanceState.STALE, 4242)
    # PID reused by an unrelated command: never treated as ours.
    assert instance_state(spec, lambda pid: "/usr/bin/vim notes.txt") == (
        InstanceState.STALE,
        4242,
    )
    _write_pid(spec, "garbage")
    assert instance_state(spec, lambda pid: _CMD) == (InstanceState.STALE, None)


def test_spawn_detaches_writes_pid_and_logs_to_data_dir(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    popen = MagicMock(return_value=MagicMock(pid=999))

    proc = spawn(spec, {"K": "V"}, tmp_path, popen=popen)

    assert proc.pid == 999
    assert spec.pid_file.read_text() == "999\n"
    args, kwargs = popen.call_args
    assert args[0] == uvicorn_command(spec)
    assert args[0][-4:] == ["--host", "127.0.0.1", "--port", "8001"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["env"] == {"K": "V"}
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdout"].name == str(uvicorn_log_path(spec))
    assert uvicorn_log_path(spec, date(2026, 1, 2)) == spec.logs_dir / "uvicorn-2026-01-02.log"


def test_wait_healthy_outcomes() -> None:
    running = MagicMock(poll=MagicMock(return_value=None))
    exited = MagicMock(poll=MagicMock(return_value=1))
    no_sleep: Any = lambda _: None  # noqa: E731

    assert wait_healthy(running, 8001, probe=lambda _: True, sleep=no_sleep) == "ok"
    assert wait_healthy(exited, 8001, probe=lambda _: False, sleep=no_sleep) == "exited"
    assert wait_healthy(running, 8001, timeout=0.0, probe=lambda _: False) == "timeout"


def test_stop_pid_graceful_and_forced() -> None:
    sent: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))

    assert stop_pid(7, kill=kill, alive=lambda _: False, sleep=lambda _: None) is True
    assert sent == [(7, signal.SIGTERM)]

    sent.clear()
    assert stop_pid(7, timeout=0.0, kill=kill, alive=lambda _: True) is False
    assert sent == [(7, signal.SIGTERM), (7, signal.SIGKILL)]


def test_stop_pid_already_gone() -> None:
    def kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    assert stop_pid(7, kill=kill) is True
