"""
Component ID: CMP_CORE_CAPABILITIES

Loads capability definitions from config/capabilities/*.yaml.
"""

import json
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


def apply_claude_code_settings(
    definitions: dict[str, CapabilityDefinition],
    enabled_capabilities: list[str],
    settings_path: Path | None = None,
) -> None:
    """Merge claude_code_settings from active capabilities into ~/.claude/settings.json.

    Permissions are union-merged: all allow/deny entries from all active capabilities
    are combined (duplicates removed, order preserved).
    """
    if settings_path is None:
        settings_path = Path.home() / ".claude" / "settings.json"

    merged_allow: list[str] = []
    merged_deny: list[str] = []
    seen_allow: set[str] = set()
    seen_deny: set[str] = set()

    for cap_id in enabled_capabilities:
        definition = definitions.get(cap_id)
        if definition is None or definition.claude_code_settings is None:
            continue
        perms = definition.claude_code_settings.permissions
        for entry in perms.allow:
            if entry not in seen_allow:
                merged_allow.append(entry)
                seen_allow.add(entry)
        for entry in perms.deny:
            if entry not in seen_deny:
                merged_deny.append(entry)
                seen_deny.add(entry)

    if not merged_allow and not merged_deny:
        return

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing.setdefault("permissions", {})
    existing["permissions"]["allow"] = merged_allow
    existing["permissions"]["deny"] = merged_deny

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
