"""
The admin edit form must not write this instance's env overrides to the shared config.

The form submits every field, so it is pre-filled from the domain file (or schema
defaults), not from the effective config. Saving an unchanged form on an instance with
``ASSISTANT_*`` overrides (e.g. one started by the instance launcher) leaves the file's
values as they were.
"""

import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from assistant.admin.auth import _SESSION_COOKIE, _serializer
from assistant.api import deps
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolsConfig,
)

_FILE_SCHEDULER = {"tick_seconds": 10, "max_jobs": 500}
_FILE_MEMORY = {"api_key": "file-key", "default_user_id": "shared"}


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="data/instances/research", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="m", model_allowlist=["m"]),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=[], denied_capabilities=[]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(tick_seconds=3),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="file-key", default_user_id="research"),
    )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    (tmp_path / "scheduler.yaml").write_text(yaml.safe_dump(_FILE_SCHEDULER))
    (tmp_path / "memory.yaml").write_text(yaml.safe_dump(_FILE_MEMORY))
    monkeypatch.setenv("ASSISTANT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_SCHEDULER_TICK_SECONDS", "3")
    monkeypatch.setenv("ASSISTANT_MEMORY_DEFAULT_USER_ID", "research")
    monkeypatch.setenv("ASSISTANT_ADMIN_TOKEN", "test-token")
    monkeypatch.setattr(deps, "_runtime_config", _runtime_config())
    with patch("assistant.core.bootstrap.bootstrap", return_value=_runtime_config()):
        from assistant.api.main import app

        test_client = TestClient(app, raise_server_exceptions=True)
    test_client.cookies.set(_SESSION_COOKIE, _serializer().dumps({"authenticated": True}))
    return test_client


def _form_values(html: str) -> dict[str, str]:
    """name -> value of the text/number inputs in the rendered form."""
    values: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and value:
            values[name.group(1)] = value.group(1)
    return values


def _read(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def test_form_is_prefilled_from_the_file_not_env(client: TestClient) -> None:
    resp = client.get("/admin/config/scheduler/form")

    assert resp.status_code == 200
    values = _form_values(resp.text)
    assert values["tick_seconds"] == "10"  # file value, not the env override 3
    assert values["max_lateness_seconds"] == "300"  # schema default
    assert "provenance-env" in resp.text  # the env-overridden field is marked

    memory = _form_values(client.get("/admin/config/memory/form").text)
    assert memory["default_user_id"] == "shared"


def test_saving_the_unchanged_form_keeps_env_values_out_of_the_file(
    client: TestClient, tmp_path: Path
) -> None:
    values = _form_values(client.get("/admin/config/scheduler/form").text)
    values["max_jobs"] = "42"  # the only edit

    diff = client.post("/admin/config/scheduler/diff", data=values)
    assert diff.status_code == 200
    assert "tick_seconds" not in diff.text
    assert "max_jobs" in diff.text

    resp = client.post("/admin/config/scheduler/apply", data=values)

    assert resp.status_code == 200
    written = _read(tmp_path / "scheduler.yaml")
    assert written["tick_seconds"] == 10
    assert written["max_jobs"] == 42
    # The live config of this instance still carries its env override.
    assert deps.get_runtime_config().scheduler.tick_seconds == 3

    memory = _form_values(client.get("/admin/config/memory/form").text)
    assert client.post("/admin/config/memory/apply", data=memory).status_code == 200
    assert _read(tmp_path / "memory.yaml")["default_user_id"] == "shared"


def test_form_reports_an_unreadable_file(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "scheduler.yaml").write_text("tick_seconds: [unclosed\n")

    resp = client.get("/admin/config/scheduler/form")

    assert resp.status_code == 200
    assert "Cannot read existing config file scheduler.yaml" in resp.text
