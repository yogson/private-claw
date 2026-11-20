"""Tests for launcher preflight: every refusal reason, checked before anything starts."""

import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

from assistant.launcher.instances import InstanceSpec, LauncherError, parse_instances
from assistant.launcher.preflight import port_in_use, preflight

_TWO = {
    "a": {"port": 8001, "token_env": "TG_A"},
    "b": {"port": 8002, "token_env": "TG_B"},
}
_TOKENS = {"TG_A": "token-a", "TG_B": "token-b", "ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN": "main"}


def _specs(root: Path, instances: dict[str, Any] | None = None) -> list[InstanceSpec]:
    return parse_instances({"instances": instances or _TWO}, root)


def _run(
    root: Path,
    specs: list[InstanceSpec],
    lookup: dict[str, str],
    busy: set[int] | None = None,
    to_start: list[InstanceSpec] | None = None,
) -> dict[str, str]:
    config_dir = root / "config.local"
    config_dir.mkdir(exist_ok=True)
    return preflight(
        specs,
        specs if to_start is None else to_start,
        lookup,
        config_dir,
        lambda port: port in (busy or set()),
    )


def _problems(*args: Any, **kwargs: Any) -> str:
    with pytest.raises(LauncherError) as exc_info:
        _run(*args, **kwargs)
    return "\n".join(exc_info.value.problems)


def test_preflight_passes_and_returns_tokens(tmp_path: Path) -> None:
    assert _run(tmp_path, _specs(tmp_path), _TOKENS) == {"a": "token-a", "b": "token-b"}


def test_preflight_missing_or_empty_token(tmp_path: Path) -> None:
    text = _problems(tmp_path, _specs(tmp_path), {"TG_A": "  "})
    assert "a: TG_A is not set or empty" in text
    assert "b: TG_B is not set or empty" in text


def test_preflight_duplicate_tokens(tmp_path: Path) -> None:
    text = _problems(tmp_path, _specs(tmp_path), {"TG_A": "same", "TG_B": "same"})
    assert "b: same bot token as instance 'a'" in text


def test_preflight_has_no_main_token_special_case(tmp_path: Path) -> None:
    """The bare bot token var and the shared YAML token are not reserved: no instance
    uses them (each child gets its own token through the override)."""
    config_dir = tmp_path / "config.local"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "channel.telegram.yaml").write_text(yaml.safe_dump({"bot_token": "token-b"}))

    tokens = _run(tmp_path, _specs(tmp_path), {**_TOKENS, "TG_A": "main"})

    assert tokens == {"a": "main", "b": "token-b"}


def test_preflight_ports(tmp_path: Path) -> None:
    specs = _specs(
        tmp_path,
        {
            "a": {"port": 8000, "token_env": "TG_A"},
            "b": {"port": 8002, "token_env": "TG_B"},
            "c": {"port": 8002, "token_env": "TG_C"},
            "d": {"port": 8004, "token_env": "TG_D"},
        },
    )
    lookup = {"TG_A": "1", "TG_B": "2", "TG_C": "3", "TG_D": "4"}
    text = _problems(tmp_path, specs, lookup, busy={8004})
    assert "a:" not in text
    assert "c: port 8002 also used by 'b'" in text
    assert "d: port 8004 is already in use" in text


def test_preflight_port_8000_is_an_ordinary_port(tmp_path: Path) -> None:
    specs = _specs(tmp_path, {"main": {"port": 8000, "token_env": "TG_A"}})
    assert _run(tmp_path, specs, _TOKENS) == {"main": "token-a"}
    # Busy 8000 (e.g. an old `make start` process) blocks the start like any port.
    text = _problems(tmp_path, specs, _TOKENS, busy={8000})
    assert "main: port 8000 is already in use" in text


def test_preflight_data_dirs(tmp_path: Path) -> None:
    specs = _specs(
        tmp_path,
        {
            "a": {"port": 8001, "token_env": "TG_A", "data_dir": "./data"},
            "b": {"port": 8002, "token_env": "TG_B", "data_dir": "data/x"},
            "c": {"port": 8003, "token_env": "TG_C", "data_dir": "data/x/"},
            "d": {"port": 8004, "token_env": "TG_D", "data_dir": "data"},
        },
    )
    text = _problems(tmp_path, specs, {"TG_A": "1", "TG_B": "2", "TG_C": "3", "TG_D": "4"})
    assert "b:" not in text
    assert "c: data_dir also used by 'b'" in text
    assert "d: data_dir also used by 'a'" in text


def test_preflight_accepts_classic_data_dir_next_to_default_dirs(tmp_path: Path) -> None:
    """./data is an ordinary data dir; the shared app.yaml data_root is not reserved."""
    config_dir = tmp_path / "config.local"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "app.yaml").write_text(yaml.safe_dump({"data_root": "data/instances/a"}))
    specs = _specs(
        tmp_path,
        {
            "main": {"port": 8000, "token_env": "TG_M", "data_dir": "data"},
            **_TWO,
        },
    )
    lookup = {**_TOKENS, "TG_M": "token-m", "ASSISTANT_APP_DATA_ROOT": "data"}

    assert _run(tmp_path, specs, lookup) == {"main": "token-m", "a": "token-a", "b": "token-b"}


def test_preflight_only_checks_presence_and_ports_of_instances_to_start(tmp_path: Path) -> None:
    specs = _specs(tmp_path)
    # b is already running: its port is busy (by itself) and that is fine.
    tokens = _run(tmp_path, specs, _TOKENS, busy={8002}, to_start=[specs[0]])
    assert tokens == {"a": "token-a"}
    # A running instance with the same token still blocks the start.
    text = _problems(tmp_path, specs, {"TG_A": "x", "TG_B": "x"}, busy={8002}, to_start=[specs[0]])
    assert "same bot token" in text


def _write_agent_node(root: Path, **values: Any) -> None:
    config_dir = root / "config.local"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "agent_node.yaml").write_text(yaml.safe_dump(values))


def test_preflight_skips_token_checks_for_telegram_off(tmp_path: Path) -> None:
    _write_agent_node(tmp_path, auth_token="secret")
    specs = _specs(
        tmp_path,
        {
            "ingress": {"port": 8001, "telegram": False, "agent_node": True},
            # token_env is ignored without telegram: a missing or duplicate token is fine.
            "other": {"port": 8002, "telegram": False, "agent_node": True, "token_env": "TG_M"},
            "bot": {"port": 8003, "token_env": "TG_B"},
        },
    )
    lookup = {"TG_B": "token-b", "TG_M": "token-b"}

    assert _run(tmp_path, specs, lookup) == {"ingress": "", "other": "", "bot": "token-b"}


def test_preflight_rejects_ingress_only_instance_without_agent_node_config(
    tmp_path: Path,
) -> None:
    specs = _specs(tmp_path, {"jobs": {"port": 8001, "telegram": False, "agent_node": True}})

    text = _problems(tmp_path, specs, {})
    assert "jobs: agent_node: true but" in text and "agent_node.yaml does not exist" in text
    assert "it would serve nothing" in text

    _write_agent_node(tmp_path, repositories={})
    text = _problems(tmp_path, specs, {})
    assert "jobs: agent_node: true but auth_token is empty" in text
    assert "ASSISTANT_AGENT_NODE_AUTH_TOKEN" in text


def test_preflight_ingress_only_accepts_token_from_yaml_or_env(tmp_path: Path) -> None:
    specs = _specs(tmp_path, {"jobs": {"port": 8001, "telegram": False, "agent_node": True}})
    _write_agent_node(tmp_path, auth_token="secret")
    assert _run(tmp_path, specs, {}) == {"jobs": ""}

    _write_agent_node(tmp_path)
    assert _run(tmp_path, specs, {"ASSISTANT_AGENT_NODE_AUTH_TOKEN": "from-env"}) == {"jobs": ""}


def test_preflight_ingress_check_skips_telegram_instances(tmp_path: Path) -> None:
    specs = _specs(tmp_path, {"a": {"port": 8001, "token_env": "TG_A", "agent_node": True}})

    assert _run(tmp_path, specs, _TOKENS) == {"a": "token-a"}


def test_preflight_still_checks_tokens_of_telegram_instances(tmp_path: Path) -> None:
    _write_agent_node(tmp_path, auth_token="secret")
    specs = _specs(
        tmp_path,
        {
            "ingress": {"port": 8001, "telegram": False, "agent_node": True},
            "a": {"port": 8002, "telegram": True, "token_env": "TG_A"},
            "b": {"port": 8003, "token_env": "TG_B"},
            "c": {"port": 8004, "token_env": "TG_C", "agent_node": True},
        },
    )
    lookup = {"TG_B": "shared", "TG_C": "shared"}

    text = _problems(tmp_path, specs, lookup)

    assert "ingress" not in text
    assert "a: TG_A is not set or empty" in text
    assert "c: same bot token as instance 'b'" in text


def test_port_in_use_detects_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert port_in_use(server.getsockname()[1]) is True


def test_port_in_use_false_for_free_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port_in_use(port) is False
