"""Startup apply of ~/.claude/settings.json: never aborts boot, a skipped write is retried."""

import errno
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from structlog.testing import capture_logs

from assistant.api.lifespan import _lifespan
from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    ClaudeCodePermissions,
    ClaudeCodeSettings,
)
from assistant.core.config.schemas import CapabilitiesPolicyConfig
from tests.assistant.api.test_telegram_lifecycle import _runtime_config
from tests.assistant.capability_helpers import StaticCatalog, snapshot_of

_DEFINITIONS = {
    "a": CapabilityDefinition(
        capability_id="a",
        claude_code_settings=ClaudeCodeSettings(permissions=ClaudeCodePermissions(allow=["Read"])),
    )
}


@pytest.mark.parametrize("failure", ["invalid_json", "permission_denied", "disk_full"])
async def test_skipped_startup_write_does_not_abort_boot_and_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    home = tmp_path / "home"
    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config = _runtime_config(allowlist=[1])
    config.telegram.enabled = False
    config.capabilities = CapabilitiesPolicyConfig(enabled_capabilities=["a"])
    catalog = StaticCatalog(snapshot_of(_DEFINITIONS))

    with ExitStack() as stack:
        stack.enter_context(
            patch("assistant.api.lifespan.bootstrap_with_catalog", return_value=(config, catalog))
        )
        stack.enter_context(patch("assistant.api.lifespan.configure_logging", return_value="x"))
        stack.enter_context(patch("assistant.api.lifespan.configure_logfire", return_value=False))
        write_failure = stack.enter_context(ExitStack())
        if failure == "invalid_json":
            settings_path.write_text("{broken")
        elif failure == "permission_denied":
            error = PermissionError(errno.EACCES, "Permission denied")
            write_failure.enter_context(patch("tempfile.mkstemp", side_effect=error))
        else:
            error = OSError(errno.ENOSPC, "No space left on device")
            write_failure.enter_context(patch("os.replace", side_effect=error))
        logs = stack.enter_context(capture_logs())

        async with _lifespan(FastAPI()):  # startup completes despite the skipped write
            assert "capabilities.claude_code_settings.skipped" in [e["event"] for e in logs]
            assert not settings_path.exists() or settings_path.read_text() == "{broken"
            # The cause is fixed; the next revision does not change claude_code_settings,
            # yet the write is retried because the startup apply was never recorded.
            write_failure.close()
            settings_path.unlink(missing_ok=True)
            catalog.snapshot = snapshot_of(_DEFINITIONS, revision=2)
            for subscriber in catalog._subscribers:
                subscriber(catalog.snapshot)
            assert json.loads(settings_path.read_text())["permissions"]["allow"] == ["Read"]
