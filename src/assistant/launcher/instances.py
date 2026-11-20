"""
Component ID: CMP_OPS_INSTANCE_LAUNCHER

Instance specs: parse ``<config_dir>/instances.yaml``, preflight-check every
instance before any is started, and build each child process environment.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

DATA_DIR = "data"
BOT_TOKEN_ENV = "ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN"
INSTANCE_HOST = "127.0.0.1"
INSTANCES_FILENAME = "instances.yaml"

CONFIG_DIR_ENV = "ASSISTANT_CONFIG_DIR"
DATA_ROOT_ENV = "ASSISTANT_APP_DATA_ROOT"
INSTANCE_NAME_ENV = "ASSISTANT_APP_INSTANCE_NAME"
AGENT_NODE_ENABLED_ENV = "ASSISTANT_AGENT_NODE_ENABLED"
TELEGRAM_ENABLED_ENV = "ASSISTANT_CHANNEL_TELEGRAM_ENABLED"
LOGFIRE_SERVICE_ENV = "LOGFIRE_SERVICE_NAME"
_DEFAULT_CONFIG_DIR = "config"
# Set by the launcher itself; `env:` extras may not override them.
_RESERVED_ENV = {
    CONFIG_DIR_ENV,
    BOT_TOKEN_ENV,
    DATA_ROOT_ENV,
    INSTANCE_NAME_ENV,
    AGENT_NODE_ENABLED_ENV,
    TELEGRAM_ENABLED_ENV,
}
_ALLOWED_KEYS = {"port", "token_env", "data_dir", "telegram", "agent_node", "env", "instance_name"}
# Instance names and labels: the label reaches the app as ASSISTANT_APP_INSTANCE_NAME,
# which is JSON-parsed, so it must not parse as a number, bool or null.
_NAME_RE = re.compile(r"(?!(?:true|false|null)\Z)[a-z][a-z0-9_-]{0,31}")
_NAME_RULE = "must be 1-32 chars [a-z0-9_-], start with a letter, not true/false/null"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LauncherError(Exception):
    """Invalid instances.yaml or failed preflight; the message lists every problem."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(f"  - {p}" for p in self.problems))


@dataclass(frozen=True)
class InstanceSpec:
    """One instance from instances.yaml (paths are absolute)."""

    name: str
    port: int
    token_env: str | None  # None when telegram is off (token_env is then ignored)
    data_dir: Path
    instance_name: str
    telegram: bool = True
    agent_node: bool = False
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def pid_file(self) -> Path:
        """PID file of this instance's uvicorn process."""
        return self.data_dir / "run" / "uvicorn.pid"

    @property
    def logs_dir(self) -> Path:
        """Directory for uvicorn and app logs of this instance."""
        return self.data_dir / "logs"


def load_env_lookup(repo_root: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """Variables visible to the app: the process env wins over ``.env`` (like load_dotenv)."""
    dotenv_path = repo_root / ".env"
    from_file = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}
    return {**{k: v for k, v in from_file.items() if v is not None}, **environ}


def resolve_shared_config_dir(lookup: Mapping[str, str], repo_root: Path) -> Path:
    """The shared config dir (ASSISTANT_CONFIG_DIR, default ``config``)."""
    raw = lookup.get(CONFIG_DIR_ENV, "").strip() or _DEFAULT_CONFIG_DIR
    return absolute_path(raw, repo_root)


def load_instances(config_dir: Path, repo_root: Path) -> list[InstanceSpec]:
    """Read and validate ``<config_dir>/instances.yaml``."""
    path = config_dir / INSTANCES_FILENAME
    if not path.is_file():
        raise LauncherError(
            [f"{path} not found; copy config/instances.example.yaml there and edit it"]
        )
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise LauncherError([f"{path}: YAML parse error: {exc}"]) from exc
    return parse_instances(data, repo_root)


def parse_instances(data: Any, repo_root: Path) -> list[InstanceSpec]:
    """Validate the instances.yaml mapping; raises LauncherError listing all problems."""
    if data is None:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("instances") or {}, dict):
        raise LauncherError(["instances.yaml must contain a mapping 'instances: {<name>: ...}'"])
    problems: list[str] = []
    specs: list[InstanceSpec] = []
    for name, raw in (data.get("instances") or {}).items():
        if not isinstance(name, str):
            problems.append(
                f"{name!r}: instance name must be a string; quote it "
                "(YAML reads unquoted yes/no/on/off/null and numbers as non-strings)"
            )
            continue
        spec = _parse_one(name, raw, repo_root, problems)
        if spec is not None:
            specs.append(spec)
    if problems:
        raise LauncherError(problems)
    return specs


def _parse_one(name: str, raw: Any, repo_root: Path, problems: list[str]) -> InstanceSpec | None:
    start = len(problems)
    if not _NAME_RE.fullmatch(name):
        problems.append(f"{name}: instance name {_NAME_RULE}")
    if not isinstance(raw, dict):
        problems.append(f"{name}: expected a mapping with at least port (and token_env)")
        return None
    for key in sorted(set(raw) - _ALLOWED_KEYS):
        hint = " (instances always bind 127.0.0.1)" if key == "host" else ""
        problems.append(f"{name}: unknown key '{key}'{hint}")

    port = raw.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append(f"{name}: port must be an integer 1-65535")
    telegram = raw.get("telegram", True)
    if not isinstance(telegram, bool):
        problems.append(f"{name}: telegram must be a boolean")
    # Without the Telegram channel the bot token is not used, so token_env is ignored.
    token_env = raw.get("token_env") if telegram is True else None
    if telegram is True:
        if not isinstance(token_env, str) or not _ENV_NAME_RE.match(token_env):
            problems.append(f"{name}: token_env must be an env variable name")
        elif token_env.upper().startswith("ASSISTANT_"):
            problems.append(
                f"{name}: token_env '{token_env}' must not start with ASSISTANT_ "
                "(the app would read it as a config override)"
            )
    data_dir = raw.get("data_dir", f"{DATA_DIR}/instances/{name}")
    if not isinstance(data_dir, str) or not data_dir.strip():
        problems.append(f"{name}: data_dir must be a non-empty path")
    agent_node = raw.get("agent_node", False)
    if not isinstance(agent_node, bool):
        problems.append(f"{name}: agent_node must be true or false")
    if telegram is False and agent_node is False:
        problems.append(
            f"{name}: telegram and agent_node are both false; "
            "the instance would serve nothing, enable at least one"
        )
    label = raw.get("instance_name", name)
    # An unset label defaults to the (already validated) name; don't report it twice.
    if "instance_name" in raw and (not isinstance(label, str) or not _NAME_RE.fullmatch(label)):
        problems.append(f"{name}: instance_name {_NAME_RULE}")
    env = _parse_env(name, raw.get("env") or {}, problems)

    if len(problems) > start or not isinstance(port, int):
        return None
    return InstanceSpec(
        name=name,
        port=port,
        token_env=token_env,
        data_dir=absolute_path(data_dir, repo_root),
        instance_name=label,
        telegram=telegram,
        agent_node=agent_node,
        env=env,
    )


def _parse_env(name: str, raw: Any, problems: list[str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        problems.append(f"{name}: env must be a mapping of NAME: value")
        return {}
    env: dict[str, str] = {}
    for key, value in raw.items():
        key = str(key)
        if not _ENV_NAME_RE.match(key):
            problems.append(f"{name}: env key '{key}' is not a valid variable name")
        elif key.upper().split("__")[0] in _RESERVED_ENV:
            problems.append(f"{name}: env key '{key}' is set by the launcher; use the YAML keys")
        elif isinstance(value, dict | list) or value is None:
            problems.append(f"{name}: env value for '{key}' must be a scalar")
        else:
            env[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return env


def build_child_env(
    spec: InstanceSpec,
    token: str,
    base_env: Mapping[str, str],
    config_dir: Path,
    all_specs: Iterable[InstanceSpec],
) -> dict[str, str]:
    """Environment for one instance: ``base_env`` plus the per-instance overrides.

    Every instance's token variable is dropped from the inherited env; the child's own
    token goes in through the app's config override. (The child's ``load_dotenv()`` still
    reads every token listed in ``.env``.) With ``telegram: false`` the channel is switched
    off and the bot token override is empty. Either way the override replaces any bare
    ``ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN`` from the env or ``.env`` and the shared YAML
    ``bot_token``, so no instance inherits a token it was not given.
    """
    token_envs = {s.token_env for s in all_specs if s.token_env}
    env = {k: v for k, v in base_env.items() if k not in token_envs}
    env[LOGFIRE_SERVICE_ENV] = f"private-claw-{spec.name}"
    env.update(spec.env)
    env[CONFIG_DIR_ENV] = str(config_dir)
    env[TELEGRAM_ENABLED_ENV] = _env_bool(spec.telegram)
    env[BOT_TOKEN_ENV] = token if spec.telegram else ""
    env[AGENT_NODE_ENABLED_ENV] = _env_bool(spec.agent_node)
    env[DATA_ROOT_ENV] = str(spec.data_dir)
    env[INSTANCE_NAME_ENV] = spec.instance_name
    return env


def _env_bool(value: bool) -> str:
    return "true" if value else "false"


def absolute_path(path: str, repo_root: Path) -> Path:
    """``path`` resolved against ``repo_root`` when relative."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()
