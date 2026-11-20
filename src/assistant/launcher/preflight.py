"""
Component ID: CMP_OPS_INSTANCE_LAUNCHER

Preflight for ``make up``: validate every instance before any is started (tokens,
ports, data dirs). All instances are equal: no port, token or data dir is reserved.
"""

import socket
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from assistant.launcher.instances import (
    INSTANCE_HOST,
    InstanceSpec,
    LauncherError,
)

AGENT_NODE_TOKEN_ENV = "ASSISTANT_AGENT_NODE_AUTH_TOKEN"


def preflight(
    specs: list[InstanceSpec],
    to_start: list[InstanceSpec],
    lookup: Mapping[str, str],
    config_dir: Path,
    is_port_busy: Callable[[int], bool],
) -> dict[str, str]:
    """Check every instance before anything starts; return the tokens of ``to_start``.

    Cross-instance rules (unique ports, tokens, data dirs) apply to the whole file.
    Token presence and free ports are checked for the instances about to start.
    Instances with ``telegram: false`` have no token: they skip the token checks and
    get ``""``; if they are also ingress-only (``agent_node: true``), the agent-node
    config must be usable, or they would serve nothing. Raises LauncherError listing
    every problem.
    """
    problems: list[str] = []
    tokens = {s.name: lookup.get(s.token_env, "").strip() for s in specs if s.token_env}

    for spec in to_start:
        if spec.token_env and not tokens[spec.name]:
            problems.append(
                f"{spec.name}: {spec.token_env} is not set or empty (process env or .env)"
            )
        if is_port_busy(spec.port):
            problems.append(f"{spec.name}: port {spec.port} is already in use")
        if not spec.telegram and spec.agent_node:
            ingress_problem = _ingress_problem(lookup, config_dir)
            if ingress_problem:
                problems.append(f"{spec.name}: {ingress_problem}; it would serve nothing")

    seen_tokens: dict[str, str] = {}
    seen_ports: dict[int, str] = {}
    seen_dirs: dict[Path, str] = {}
    for spec in specs:
        token = tokens.get(spec.name, "")
        if token and token in seen_tokens:
            problems.append(f"{spec.name}: same bot token as instance '{seen_tokens[token]}'")
        elif token:
            seen_tokens[token] = spec.name
        if spec.port in seen_ports:
            problems.append(f"{spec.name}: port {spec.port} also used by '{seen_ports[spec.port]}'")
        seen_ports.setdefault(spec.port, spec.name)
        if spec.data_dir in seen_dirs:
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


def _ingress_problem(lookup: Mapping[str, str], config_dir: Path) -> str | None:
    """Why the shared agent-node ingress cannot come up, or None when it can."""
    path = config_dir / "agent_node.yaml"
    if not path.is_file():
        return f"agent_node: true but {path} does not exist"
    token = lookup.get(AGENT_NODE_TOKEN_ENV, "") or _yaml_value(path, "auth_token") or ""
    if not isinstance(token, str) or not token.strip():
        return f"agent_node: true but auth_token is empty (set {AGENT_NODE_TOKEN_ENV} or {path})"
    return None


def _yaml_value(path: Path, key: str) -> Any:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    return data.get(key) if isinstance(data, dict) else None
