"""Tests for the launcher CLI: up/down/status with subprocesses and signals mocked."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from assistant.launcher import cli
from assistant.launcher.process import InstanceState, instance_state

_CMD = "/repo/.venv/bin/python -m uvicorn assistant.api.main:app --host 127.0.0.1 --port {port}"
_SECRET_A = "111:secret-token-a"
_SECRET_B = "222:secret-token-b"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config.local"
    config_dir.mkdir()
    (config_dir / "instances.yaml").write_text(
        yaml.safe_dump(
            {
                "instances": {
                    "a": {"port": 8001, "token_env": "TG_A"},
                    "b": {"port": 8002, "token_env": "TG_B"},
                }
            }
        )
    )
    (tmp_path / ".env").write_text(
        f"ASSISTANT_CONFIG_DIR=config.local\nTG_A={_SECRET_A}\nTG_B={_SECRET_B}\n"
    )
    monkeypatch.setattr(cli, "port_in_use", lambda port: False)
    monkeypatch.setattr(cli, "wait_healthy", lambda proc, port: "ok")
    monkeypatch.setattr(cli, "health_live", lambda port: True)
    return tmp_path


def _spawn_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_spawn(spec: Any, env: dict[str, str], repo_root: Path) -> Any:
        calls.append((spec.name, env))
        spec.pid_file.parent.mkdir(parents=True, exist_ok=True)
        spec.pid_file.write_text(f"{1000 + spec.port}\n")
        return MagicMock(pid=1000 + spec.port)

    monkeypatch.setattr(cli, "spawn", fake_spawn)
    return calls


def _running_commands(monkeypatch: pytest.MonkeyPatch, commands: dict[int, str]) -> None:
    monkeypatch.setattr(cli, "instance_state", lambda spec: instance_state(spec, commands.get))


def test_up_starts_all_with_shared_config_dir(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _spawn_recorder(monkeypatch)

    assert cli.main(["up"], environ={"PATH": "/bin"}, repo_root=repo) == 0

    assert [name for name, _ in calls] == ["a", "b"]
    env_a = dict(calls[0][1])
    assert env_a["ASSISTANT_CONFIG_DIR"] == str((repo / "config.local").resolve())
    assert env_a["ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN"] == _SECRET_A
    assert env_a["ASSISTANT_APP_DATA_ROOT"] == str((repo / "data/instances/a").resolve())
    out = capsys.readouterr().out
    assert "a: started, pid 9001" in out
    assert _SECRET_A not in out and _SECRET_B not in out


def test_up_refuses_everything_when_one_instance_is_invalid(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _spawn_recorder(monkeypatch)

    # b reuses a's token: refuse, and start nothing (not even a).
    code = cli.main(["up"], environ={"TG_B": _SECRET_A}, repo_root=repo)

    assert code == 2
    assert calls == []
    err = capsys.readouterr().err
    assert "refused, nothing started" in err
    assert "b: same bot token as instance 'a'" in err
    assert _SECRET_A not in err


def test_up_single_instance_skips_running(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _spawn_recorder(monkeypatch)
    _running_commands(monkeypatch, {9001: _CMD.format(port=8001)})
    pid_file = repo / "data/instances/a/run/uvicorn.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("9001")

    assert cli.main(["up", "a"], environ={}, repo_root=repo) == 0
    assert calls == []
    assert "a: already running (pid 9001" in capsys.readouterr().out

    assert cli.main(["up", "b"], environ={}, repo_root=repo) == 0
    assert [name for name, _ in calls] == ["b"]


def test_up_reports_startup_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _spawn_recorder(monkeypatch)
    monkeypatch.setattr(cli, "wait_healthy", lambda proc, port: "exited" if port == 8002 else "ok")

    assert cli.main(["up"], environ={}, repo_root=repo) == 1
    assert (repo / "data/instances/a/run/uvicorn.pid").exists()
    assert not (repo / "data/instances/b/run/uvicorn.pid").exists()


def test_unknown_instance_name(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["down", "nope"], environ={}, repo_root=repo) == 2
    assert "unknown instance 'nope' (configured: a, b)" in capsys.readouterr().err


def test_down_stops_only_our_processes(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # a runs; b's PID was reused by an unrelated process.
    _running_commands(monkeypatch, {9001: _CMD.format(port=8001), 9002: "/usr/bin/vim"})
    for name, pid in (("a", 9001), ("b", 9002)):
        pid_file = repo / f"data/instances/{name}/run/uvicorn.pid"
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text(str(pid))
    stopped: list[int] = []
    monkeypatch.setattr(cli, "stop_pid", lambda pid: stopped.append(pid) or True)

    assert cli.main(["down"], environ={}, repo_root=repo) == 0

    assert stopped == [9001]
    assert not (repo / "data/instances/a/run/uvicorn.pid").exists()
    assert not (repo / "data/instances/b/run/uvicorn.pid").exists()
    out = capsys.readouterr().out
    assert "a: stopped" in out
    assert "b: removed stale PID file" in out


def test_down_single_instance(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _running_commands(monkeypatch, {9001: _CMD.format(port=8001), 9002: _CMD.format(port=8002)})
    for name, pid in (("a", 9001), ("b", 9002)):
        pid_file = repo / f"data/instances/{name}/run/uvicorn.pid"
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text(str(pid))
    stopped: list[int] = []
    monkeypatch.setattr(cli, "stop_pid", lambda pid: stopped.append(pid) or True)

    assert cli.main(["down", "b"], environ={}, repo_root=repo) == 0
    assert stopped == [9002]
    assert (repo / "data/instances/a/run/uvicorn.pid").exists()


def test_status_shows_states_without_token_values(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _running_commands(monkeypatch, {9001: _CMD.format(port=8001)})
    pid_file = repo / "data/instances/a/run/uvicorn.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("9001")

    assert cli.main(["status"], environ={}, repo_root=repo) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "NAME",
        "PORT",
        "STATE",
        "PID",
        "HEALTH",
        "TOKEN_ENV",
        "DATA_DIR",
        "LOG",
    ]
    row_a, row_b = lines[1].split(), lines[2].split()
    assert row_a[:6] == ["a", "8001", InstanceState.RUNNING.value, "9001", "ok", "TG_A"]
    assert row_a[6] == "data/instances/a"
    assert row_a[7].startswith("data/instances/a/logs/uvicorn-")
    assert row_b[:6] == ["b", "8002", InstanceState.STOPPED.value, "-", "-", "TG_B"]
    joined = "\n".join(lines)
    assert _SECRET_A not in joined and _SECRET_B not in joined


def test_missing_instances_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"], environ={}, repo_root=tmp_path) == 2
    assert "instances.example.yaml" in capsys.readouterr().err


def test_up_runs_an_instance_on_8000_with_the_classic_data_dir(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "config.local" / "instances.yaml").write_text(
        yaml.safe_dump(
            {"instances": {"main": {"port": 8000, "token_env": "TG_A", "data_dir": "data"}}}
        )
    )
    calls = _spawn_recorder(monkeypatch)

    assert cli.main(["up"], environ={}, repo_root=repo) == 0

    (name, env) = calls[0]
    assert name == "main"
    assert env["ASSISTANT_APP_DATA_ROOT"] == str((repo / "data").resolve())
    assert env["ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN"] == _SECRET_A
    assert (repo / "data/run/uvicorn.pid").read_text().strip() == "9000"


def test_up_notes_a_leftover_bare_bot_token_and_does_not_pass_it_on(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _spawn_recorder(monkeypatch)
    with (repo / ".env").open("a") as env_file:
        env_file.write("ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN=333:old-main-token\n")

    assert cli.main(["up"], environ={}, repo_root=repo) == 0

    out = capsys.readouterr().out
    assert "note: ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN is set" in out
    assert "old-main-token" not in out
    tokens = [env["ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN"] for _, env in calls]
    assert tokens == [_SECRET_A, _SECRET_B]


@pytest.mark.parametrize(
    ("key", "reserved"),
    [
        ("assistant_channel_telegram_bot_token", "ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN"),
        ("assistant_app_data_root", "ASSISTANT_APP_DATA_ROOT"),
        ("Assistant_Channel_Telegram_Enabled", "ASSISTANT_CHANNEL_TELEGRAM_ENABLED"),
        ("assistant_agent_node_enabled", "ASSISTANT_AGENT_NODE_ENABLED"),
        ("assistant_app_instance_name", "ASSISTANT_APP_INSTANCE_NAME"),
        ("ASSISTANT_APP_DATA_ROOT__X", "ASSISTANT_APP_DATA_ROOT"),
    ],
)
@pytest.mark.parametrize("in_dotenv", [True, False])
def test_up_refuses_case_variants_of_launcher_variables(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    reserved: str,
    in_dotenv: bool,
) -> None:
    """The app matches ASSISTANT_* case-insensitively and the child's load_dotenv() adds
    the variant after the launcher's variable, so it would win: refuse it up front."""
    calls = _spawn_recorder(monkeypatch)
    environ: dict[str, str] = {}
    if in_dotenv:
        with (repo / ".env").open("a") as env_file:
            env_file.write(f"{key}=333:leaked\n")
    else:
        environ[key] = "333:leaked"

    assert cli.main(["up"], environ=environ, repo_root=repo) == 2

    assert calls == []
    err = capsys.readouterr().err
    assert f"'{key}' (process env or .env) would override {reserved}" in err
    assert "leaked" not in err


@pytest.mark.parametrize("target", ["start", "stop"])
def test_check_single_refuses_when_instances_file_exists(
    repo: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    assert cli.main(["check-single", target], environ={}, repo_root=repo) == 2

    err = capsys.readouterr().err
    assert f"make {target}: refused: config.local/instances.yaml exists" in err
    assert "make up / make down / make status" in err


def test_check_single_passes_without_instances_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".env").write_text("ASSISTANT_CONFIG_DIR=config.local\n")
    (tmp_path / "config.local").mkdir()
    (tmp_path / "config").mkdir()
    # instances.yaml in the default config dir does not count when another one is active.
    (tmp_path / "config" / "instances.yaml").write_text("instances: {}\n")

    assert cli.main(["check-single", "start"], environ={}, repo_root=tmp_path) == 0
    assert capsys.readouterr() == ("", "")
