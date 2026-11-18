"""
Component ID: CMP_CORE_CAPABILITIES

Loads capability definitions from config/capabilities/*.yaml.
"""

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.loader import resolve_config_dir


class CapabilityLoadError(Exception):
    """Raised when capability manifest loading or validation fails."""


class ManifestParseError(Exception):
    """Raised when a single manifest file fails YAML parsing or schema validation."""

    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


def discover_capability_manifests(capabilities_dir: Path) -> list[Path]:
    """Discover capability manifest paths under config/capabilities/*.yaml."""
    if not capabilities_dir.is_dir():
        return []
    paths: list[Path] = []
    for path in sorted(capabilities_dir.iterdir()):
        if path.suffix in (".yaml", ".yml") and path.name != "index.yaml":
            paths.append(path)
    return paths


def format_validation_error(file_name: str, exc: ValidationError) -> list[str]:
    """Render a manifest ``ValidationError`` as ``[file] loc: message`` lines."""
    messages: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = "extra fields not permitted" if err["type"] == "extra_forbidden" else err["msg"]
        messages.append(f"[{file_name}] {loc}: {msg}")
    return messages


def parse_capability_manifest(file_name: str, raw: bytes) -> CapabilityDefinition:
    """Parse and validate one manifest's raw bytes.

    Raises :class:`ManifestParseError` carrying ``[file] ...`` messages on YAML, shape or
    schema errors. This is the single parser used by the loader and the catalog.
    """
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ManifestParseError([f"[{file_name}] YAML parse error: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ManifestParseError([f"[{file_name}] expected dict, got {type(data).__name__}"])
    try:
        return CapabilityDefinition(**data)
    except ValidationError as exc:
        raise ManifestParseError(format_validation_error(file_name, exc)) from exc


def load_capability_definitions(
    config_dir: str | Path | None = None,
) -> dict[str, CapabilityDefinition]:
    """Load all capability manifests from config/capabilities/*.yaml.

    Returns dict keyed by capability_id. Raises CapabilityLoadError on validation failure.
    Low-level, uncached parser: runtime code reads manifests through
    :class:`~assistant.core.capabilities.catalog.CapabilityCatalog` instead.
    """
    root = resolve_config_dir(config_dir)
    capabilities_dir = root / "capabilities"
    if not capabilities_dir.is_dir():
        return {}

    errors: list[str] = []
    definitions: dict[str, CapabilityDefinition] = {}

    for path in discover_capability_manifests(capabilities_dir):
        try:
            definition = parse_capability_manifest(path.name, path.read_bytes())
        except ManifestParseError as exc:
            errors.extend(exc.messages)
            continue
        if definition.capability_id in definitions:
            errors.append(f"[{path.name}] duplicate capability_id: {definition.capability_id}")
            continue
        definitions[definition.capability_id] = definition

    if errors:
        raise CapabilityLoadError(
            "Capability manifest validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return definitions


def collect_mcp_server_ids(
    definitions: Mapping[str, CapabilityDefinition],
    enabled_capabilities: list[str],
) -> list[str]:
    """Return the MCP server ids granted by the given capabilities (first-seen order, deduped).

    ``enabled_capabilities`` must already be nested-expanded and denied-filtered, as with the
    other collectors.
    """
    server_ids: list[str] = []
    for cap_id in enabled_capabilities:
        definition = definitions.get(cap_id)
        if definition is None:
            continue
        for server_id in definition.mcp_servers:
            if server_id not in server_ids:
                server_ids.append(server_id)
    return server_ids
