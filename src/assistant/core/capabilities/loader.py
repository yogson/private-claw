"""
Component ID: CMP_CORE_CAPABILITIES

Loads capability definitions from config/capabilities/*.yaml.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.loader import resolve_config_dir


class CapabilityLoadError(Exception):
    """Raised when capability manifest loading or validation fails."""


def discover_capability_manifests(capabilities_dir: Path) -> list[Path]:
    """Discover capability manifest paths under config/capabilities/*.yaml."""
    if not capabilities_dir.is_dir():
        return []
    paths: list[Path] = []
    for path in sorted(capabilities_dir.iterdir()):
        if path.suffix in (".yaml", ".yml") and path.name != "index.yaml":
            paths.append(path)
    return paths


def load_capability_definitions(
    config_dir: str | Path | None = None,
) -> dict[str, CapabilityDefinition]:
    """Load all capability manifests from config/capabilities/*.yaml.

    Returns dict keyed by capability_id. Raises CapabilityLoadError on validation failure.
    """
    root = resolve_config_dir(config_dir)
    capabilities_dir = root / "capabilities"
    if not capabilities_dir.is_dir():
        return {}

    errors: list[str] = []
    definitions: dict[str, CapabilityDefinition] = {}

    for path in discover_capability_manifests(capabilities_dir):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                errors.append(f"[{path.name}] expected dict, got {type(data).__name__}")
                continue
            definition = CapabilityDefinition(**data)
            if definition.capability_id in definitions:
                errors.append(f"[{path.name}] duplicate capability_id: {definition.capability_id}")
                continue
            definitions[definition.capability_id] = definition
        except yaml.YAMLError as exc:
            errors.append(f"[{path.name}] YAML parse error: {exc}")
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(x) for x in err["loc"])
                errors.append(f"[{path.name}] {loc}: {err['msg']}")

    if errors:
        raise CapabilityLoadError(
            "Capability manifest validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return definitions
