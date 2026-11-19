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
        root,
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


def test_preflight_main_token_from_env_or_shared_yaml(tmp_path: Path) -> None:
    text = _problems(tmp_path, _specs(tmp_path), {**_TOKENS, "TG_A": "main"})
    assert "a: TG_A is the main instance's bot token" in text

    config_dir = tmp_path / "config.local"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "channel.telegram.yaml").write_text(yaml.safe_dump({"bot_token": "token-b"}))
    text = _problems(tmp_path, _specs(tmp_path), _TOKENS)
    assert "b: TG_B is the main instance's bot token" in text


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
    assert "a: port 8000 is reserved for the main instance" in text
    assert "c: port 8002 also used by 'b'" in text
    assert "d: port 8004 is already in use" in text


def test_preflight_data_dirs(tmp_path: Path) -> None:
    specs = _specs(
        tmp_path,
        {
            "a": {"port": 8001, "token_env": "TG_A", "data_dir": "./data"},
            "b": {"port": 8002, "token_env": "TG_B", "data_dir": "data/x"},
            "c": {"port": 8003, "token_env": "TG_C", "data_dir": "data/x/"},
        },
    )
    text = _problems(tmp_path, specs, {"TG_A": "1", "TG_B": "2", "TG_C": "3"})
    assert "a: data_dir" in text and "is the main instance's data" in text
    assert "c: data_dir also used by 'b'" in text


def test_preflight_main_data_root_from_shared_app_yaml(tmp_path: Path) -> None:
    config_dir = tmp_path / "config.local"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "app.yaml").write_text(yaml.safe_dump({"data_root": "data/instances/a"}))
    text = _problems(tmp_path, _specs(tmp_path), _TOKENS)
    assert "a: data_dir" in text and "is the main instance's data" in text


def test_preflight_only_checks_presence_and_ports_of_instances_to_start(tmp_path: Path) -> None:
    specs = _specs(tmp_path)
    # b is already running: its port is busy (by itself) and that is fine.
    tokens = _run(tmp_path, specs, _TOKENS, busy={8002}, to_start=[specs[0]])
    assert tokens == {"a": "token-a"}
    # A running instance with the same token still blocks the start.
    text = _problems(tmp_path, specs, {"TG_A": "x", "TG_B": "x"}, busy={8002}, to_start=[specs[0]])
    assert "same bot token" in text


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
