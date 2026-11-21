"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Startup check that configured Mem0 org/project match what the API key resolves to.
"""

from mem0 import MemoryClient

from assistant.core.config.schemas import MemoryConfig


def check_configured_scope(client: MemoryClient, config: MemoryConfig) -> None:
    """Raise if memory.org_id / memory.project_id differ from the key's resolved scope.

    mem0ai 2.x always uses the org/project the API key resolves to (populated from
    /v1/ping/ while constructing the client), so a differing configured value would
    otherwise be silently ignored.
    """
    for name in ("org_id", "project_id"):
        configured = getattr(config, name)
        resolved = getattr(client, name)
        if configured and configured != resolved:
            raise ValueError(
                f"memory.{name}={configured!r} is ignored by mem0ai 2.x; the API key "
                f"resolves to {resolved!r}. Use a key for that project or clear "
                f"memory.{name}."
            )
