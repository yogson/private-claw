"""
Component ID: CMP_OPS_INSTANCE_LAUNCHER

Process handling for extra instances: PID files, checking that a PID still belongs
to the instance's uvicorn, spawning detached uvicorn processes, stopping them, and
polling ``/health/live``.
"""

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from assistant.launcher.instances import INSTANCE_HOST, InstanceSpec

APP_TARGET = "assistant.api.main:app"


class InstanceState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    STALE = "stale"


def read_pid(pid_file: Path) -> int | None:
    """PID stored in ``pid_file``, or None if the file is missing or malformed."""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def process_command(pid: int) -> str | None:
    """Command line of ``pid`` via ``ps``, or None if no such process."""
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command = result.stdout.strip()
    return command if result.returncode == 0 and command else None


def is_instance_command(command: str, port: int) -> bool:
    """True if ``command`` is this launcher's uvicorn for ``port`` (guards reused PIDs)."""
    args = command.split()
    return (
        "uvicorn" in command
        and APP_TARGET in args
        and any(args[i] == "--port" and args[i + 1] == str(port) for i in range(len(args) - 1))
    )


def instance_state(
    spec: InstanceSpec,
    command_of: Callable[[int], str | None] = process_command,
) -> tuple[InstanceState, int | None]:
    """State of an instance from its PID file and the live process table."""
    pid = read_pid(spec.pid_file)
    if pid is None:
        state = InstanceState.STALE if spec.pid_file.exists() else InstanceState.STOPPED
        return state, None
    command = command_of(pid)
    if command is not None and is_instance_command(command, spec.port):
        return InstanceState.RUNNING, pid
    return InstanceState.STALE, pid


def uvicorn_log_path(spec: InstanceSpec, today: date | None = None) -> Path:
    """Today's uvicorn log of an instance (appended across restarts on the same day)."""
    day = (today or date.today()).isoformat()
    return spec.logs_dir / f"uvicorn-{day}.log"


def uvicorn_command(spec: InstanceSpec, python: str = sys.executable) -> list[str]:
    """The uvicorn command line for an instance, run with the current interpreter."""
    return [
        python,
        "-m",
        "uvicorn",
        APP_TARGET,
        "--host",
        INSTANCE_HOST,
        "--port",
        str(spec.port),
    ]


def spawn(
    spec: InstanceSpec,
    env: Mapping[str, str],
    repo_root: Path,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Any:
    """Start the instance detached (own session, stdin closed, output to its log)."""
    spec.logs_dir.mkdir(parents=True, exist_ok=True)
    spec.pid_file.parent.mkdir(parents=True, exist_ok=True)
    with open(uvicorn_log_path(spec), "ab") as log:
        proc = popen(
            uvicorn_command(spec),
            cwd=repo_root,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    spec.pid_file.write_text(f"{proc.pid}\n")
    return proc


def health_live(port: int, timeout: float = 1.0) -> bool:
    """True if ``GET /health/live`` on the instance answers 200."""
    url = f"http://{INSTANCE_HOST}:{port}/health/live"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def wait_healthy(
    proc: Any,
    port: int,
    timeout: float = 20.0,
    probe: Callable[[int], bool] = health_live,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Wait for a freshly spawned instance: returns 'ok', 'exited' or 'timeout'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return "exited"
        if probe(port):
            return "ok"
        sleep(0.5)
    return "timeout"


def pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_pid(
    pid: int,
    timeout: float = 30.0,
    kill: Callable[[int, int], None] = os.kill,
    alive: Callable[[int], bool] = pid_alive,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """SIGTERM ``pid`` and wait for a graceful exit; SIGKILL after ``timeout``.

    Returns True on graceful exit, False if SIGKILL was needed.
    """
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        sleep(0.5)
    try:
        kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return False
