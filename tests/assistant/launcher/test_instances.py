"""Tests for instances.yaml parsing, env lookup and child env construction."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from assistant.launcher.instances import (
    InstanceSpec,
    LauncherError,
    build_child_env,
    load_env_lookup,
    load_instances,
    parse_instances,
    resolve_shared_config_dir,
)


def _parse(instances: dict[str, Any], root: Path) -> list[InstanceSpec]:
    return parse_instances({"instances": instances}, root)


def _problems(instances: dict[str, Any], root: Path) -> list[str]:
    with pytest.raises(LauncherError) as exc_info:
        _parse(instances, root)
    return exc_info.value.problems


def test_parse_applies_defaults(tmp_path: Path) -> None:
    [spec] = _parse({"research": {"port": 8001, "token_env": "TG_RESEARCH"}}, tmp_path)

    assert spec.name == "research"
    assert spec.port == 8001
    assert spec.token_env == "TG_RESEARCH"
    assert spec.data_dir == (tmp_path / "data" / "instances" / "research").resolve()
    assert spec.instance_name == "research"
    assert spec.agent_node is False
    assert spec.env == {}
    assert spec.pid_file == spec.data_dir / "run" / "uvicorn.pid"
    assert spec.logs_dir == spec.data_dir / "logs"


def test_parse_explicit_values(tmp_path: Path) -> None:
    [spec] = _parse(
        {
            "writing": {
                "port": 8002,
                "token_env": "TG_WRITING",
                "data_dir": "/srv/writing",
                "instance_name": "Writing",
                "agent_node": True,
                "env": {"ASSISTANT_MEMORY_DEFAULT_USER_ID": "writing", "X_FLAG": True, "N": 3},
            }
        },
        tmp_path,
    )

    assert spec.data_dir == Path("/srv/writing").resolve()
    assert spec.instance_name == "Writing"
    assert spec.agent_node is True
    assert spec.env == {"ASSISTANT_MEMORY_DEFAULT_USER_ID": "writing", "X_FLAG": "true", "N": "3"}


def test_parse_empty_file_has_no_instances(tmp_path: Path) -> None:
    assert parse_instances(None, tmp_path) == []
    assert parse_instances({"instances": None}, tmp_path) == []


def test_parse_reports_every_problem(tmp_path: Path) -> None:
    problems = _problems(
        {
            "Bad_Name": {"port": 8001, "token_env": "TG_A"},
            "noport": {"token_env": "TG_B"},
            "prefixed": {"port": 8003, "token_env": "ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN_X"},
            "lowerprefix": {"port": 8004, "token_env": "assistant_tok"},
            "host": {"port": 8005, "token_env": "TG_C", "host": "0.0.0.0"},
            "reserved": {
                "port": 8006,
                "token_env": "TG_D",
                "env": {"ASSISTANT_APP_DATA_ROOT": "x"},
            },
            "boolport": {"port": True, "token_env": "TG_E"},
            "notmap": "8007",
        },
        tmp_path,
    )
    text = "\n".join(problems)

    assert "Bad_Name: instance name must match" in text
    assert "noport: port must be an integer" in text
    assert "prefixed: token_env 'ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN_X' must not start" in text
    assert "lowerprefix: token_env 'assistant_tok' must not start" in text
    assert "host: unknown key 'host' (extra instances always bind 127.0.0.1)" in text
    assert "reserved: env key 'ASSISTANT_APP_DATA_ROOT' is set by the launcher" in text
    assert "boolport: port must be an integer" in text
    assert "notmap: expected a mapping" in text


def test_load_instances_missing_file_points_to_example(tmp_path: Path) -> None:
    with pytest.raises(LauncherError) as exc_info:
        load_instances(tmp_path, tmp_path)
    assert "instances.example.yaml" in str(exc_info.value)


def test_load_instances_reads_file(tmp_path: Path) -> None:
    (tmp_path / "instances.yaml").write_text(
        yaml.safe_dump({"instances": {"a": {"port": 8001, "token_env": "TG_A"}}})
    )
    assert [s.name for s in load_instances(tmp_path, tmp_path)] == ["a"]


def test_env_lookup_prefers_process_env_over_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TG_A=from-file\nTG_B=file-only\nASSISTANT_CONFIG_DIR=cfg\n")

    lookup = load_env_lookup(tmp_path, {"TG_A": "from-process"})

    assert lookup["TG_A"] == "from-process"
    assert lookup["TG_B"] == "file-only"
    assert resolve_shared_config_dir(lookup, tmp_path) == (tmp_path / "cfg").resolve()


def test_shared_config_dir_defaults_to_config(tmp_path: Path) -> None:
    assert load_env_lookup(tmp_path, {}) == {}
    assert resolve_shared_config_dir({}, tmp_path) == (tmp_path / "config").resolve()


def test_child_env_has_exactly_the_instance_overrides(tmp_path: Path) -> None:
    specs = _parse(
        {
            "a": {
                "port": 8001,
                "token_env": "TG_A",
                "env": {"ASSISTANT_MEMORY_DEFAULT_USER_ID": "a"},
            },
            "b": {"port": 8002, "token_env": "TG_B", "agent_node": True},
        },
        tmp_path,
    )
    base = {
        "PATH": "/usr/bin",
        "TG_A": "token-a",
        "TG_B": "token-b",
        "ASSISTANT_APP_DATA_ROOT": "data",
        "ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN": "main-token",
    }
    config_dir = tmp_path / "config.local"

    env_a = build_child_env(specs[0], "token-a", base, config_dir, specs)

    assert env_a == {
        "PATH": "/usr/bin",
        "ASSISTANT_CONFIG_DIR": str(config_dir),
        "ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN": "token-a",
        "ASSISTANT_APP_DATA_ROOT": str(specs[0].data_dir),
        "ASSISTANT_APP_INSTANCE_NAME": "a",
        "ASSISTANT_AGENT_NODE_ENABLED": "false",
        "ASSISTANT_MEMORY_DEFAULT_USER_ID": "a",
        "LOGFIRE_SERVICE_NAME": "private-claw-a",
    }
    assert "token-b" not in env_a.values()

    env_b = build_child_env(specs[1], "token-b", base, config_dir, specs)
    assert "ASSISTANT_AGENT_NODE_ENABLED" not in env_b
    assert "token-a" not in env_b.values()
    assert env_b["ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN"] == "token-b"
