"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Application bootstrap: loads and validates all configuration domains at startup.
Fail-fast policy: any invalid configuration prevents the service from starting.
"""

from pathlib import Path

from assistant.core.config.loader import ConfigLoader, ConfigLoadError
from assistant.core.config.schemas import RuntimeConfig


def bootstrap(config_dir: str | Path = "config") -> RuntimeConfig:
    """Load and validate all configuration domains.

    Returns the fully validated RuntimeConfig on success.
    Raises SystemExit with an actionable report on any validation failure.
    """
    loader = ConfigLoader(config_dir=config_dir)
    try:
        runtime_config = loader.load()
    except ConfigLoadError as exc:
        raise SystemExit(str(exc)) from exc
    return runtime_config
