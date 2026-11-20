"""
Component ID: CMP_OPS_INSTANCE_LAUNCHER

Preflight for ``make up``: validate every instance before any is started (tokens,
ports, data dirs, and the values reserved for the main instance).
"""

import socket
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from assistant.launcher.instances import (
    DATA_ROOT_ENV,
    INSTANCE_HOST,
    MAIN_DATA_DIR,
    MAIN_PORT,
    MAIN_TOKEN_ENV,
    InstanceSpec,
    LauncherError,
    absolute_path,
)


def preflight(
    specs: list[InstanceSpec],
    to_start: list[InstanceSpec],
    lookup: Mapping[str, str],
    config_dir: Path,
    repo_root: Path,
    is_port_busy: Callable[[int], bool],
) -> dict[str, str]:
    """Check every instance before anything starts; return the tokens of ``to_start``.

    Cross-instance rules (unique ports, tokens, data dirs; main instance reserved)
    apply to the whole file. Token presence and free ports are checked for the
    instances about to start. Instances with ``telegram: false`` have no token: they
    skip the token checks and get ``""``. Raises LauncherError listing every problem.
    """
    problems: list[str] = []
    tokens = {s.name: lookup.get(s.token_env, "").strip() for s in specs if s.token_env}

    for spec in to_start:
        if spec.token_env and not tokens[spec.name]:
            problems.append(
                f"{spec.name}: {spec.token_env} is not set or empty (process env or .env)"
            )
        if spec.port != MAIN_PORT and is_port_busy(spec.port):
            problems.append(f"{spec.name}: port {spec.port} is already in use")

    main_tokens = _main_tokens(lookup, config_dir)
    main_dirs = _main_data_dirs(lookup, config_dir, repo_root)
    seen_tokens: dict[str, str] = {}
    seen_ports: dict[int, str] = {}
    seen_dirs: dict[Path, str] = {}
    for spec in specs:
        token = tokens.get(spec.name, "")
        if token and token in main_tokens:
            problems.append(f"{spec.name}: {spec.token_env} is the main instance's bot token")
        elif token and token in seen_tokens:
            problems.append(f"{spec.name}: same bot token as instance '{seen_tokens[token]}'")
        elif token:
            seen_tokens[token] = spec.name
        if spec.port == MAIN_PORT:
            problems.append(f"{spec.name}: port {MAIN_PORT} is reserved for the main instance")
        elif spec.port in seen_ports:
            problems.append(f"{spec.name}: port {spec.port} also used by '{seen_ports[spec.port]}'")
        seen_ports.setdefault(spec.port, spec.name)
        if spec.data_dir in main_dirs:
            problems.append(f"{spec.name}: data_dir {spec.data_dir} is the main instance's data")
        elif spec.data_dir in seen_dirs:
            problems.append(f"{spec.name}: data_dir also used by '{seen_dirs[spec.data_dir]}'")
        seen_dirs.setdefault(spec.data_dir, spec.name)

    if problems:
        raise LauncherError(problems)
    return {s.name: tokens.get(s.name, "") for s in to_start}


def port_in_use(port: int) -> bool:
    """True if something accepts connections on, or holds, ``127.0.0.1:<port>``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((INSTANCE_HOST, port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((INSTANCE_HOST, port))
        except OSError:
            return True
    return False


def _main_tokens(lookup: Mapping[str, str], config_dir: Path) -> set[str]:
    candidates = [
        lookup.get(MAIN_TOKEN_ENV, ""),
        _yaml_value(config_dir / "channel.telegram.yaml", "bot_token"),
    ]
    return {c.strip() for c in candidates if isinstance(c, str) and c.strip()}


def _main_data_dirs(lookup: Mapping[str, str], config_dir: Path, repo_root: Path) -> set[Path]:
    candidates = [
        MAIN_DATA_DIR,
        lookup.get(DATA_ROOT_ENV, ""),
        _yaml_value(config_dir / "app.yaml", "data_root"),
    ]
    return {absolute_path(c, repo_root) for c in candidates if isinstance(c, str) and c.strip()}


def _yaml_value(path: Path, key: str) -> Any:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    return data.get(key) if isinstance(data, dict) else None
