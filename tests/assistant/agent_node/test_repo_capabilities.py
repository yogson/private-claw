"""Repository -> capability mapping: capability prompts in the job context."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from assistant.core.config.schemas import AgentNodeRepositoryConfig
from tests.assistant.agent_node.conftest import AUTH, Env


def _snapshot(*definitions: CapabilityDefinition) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        revision=1,
        definitions={d.capability_id: d for d in definitions},
        files={},
        capabilities_dir=Path("/nonexistent"),
    )


def _use(env: Env, snapshot: CapabilitySnapshot, **repo_fields: Any) -> None:
    repo_cfg = env.service.config.repositories["acme/widgets"]
    updated = AgentNodeRepositoryConfig.model_validate({"path": repo_cfg.path, **repo_fields})
    env.service.config = env.service.config.model_copy(
        update={"repositories": {"acme/widgets": updated}}
    )
    env.service._capability_snapshot = lambda: snapshot


async def _objective(env: Env, key: str = "exec-1:node-1", **body: Any) -> tuple[str, Any]:
    resp = await env.client.post(
        "/jobs",
        json={"user_prompt": "Fix the bug", **body},
        headers={**AUTH, "Idempotency-Key": key},
    )
    assert resp.status_code == 202, resp.text
    task = await env.store.tasks.get(resp.json()["id"])
    assert task is not None
    return task.metadata["objective"], task.metadata


class TestRepositoryConfig:
    def test_single_capability_shorthand(self) -> None:
        cfg = AgentNodeRepositoryConfig.model_validate({"path": "/r", "capability": "opergraphs"})
        assert cfg.capabilities == ["opergraphs"]

    def test_shorthand_and_list_merge_without_duplicates(self) -> None:
        cfg = AgentNodeRepositoryConfig.model_validate(
            {"path": "/r", "capability": "a", "capabilities": ["b", "a"]}
        )
        assert cfg.capabilities == ["a", "b"]

    def test_shorthand_accepts_a_list(self) -> None:
        cfg = AgentNodeRepositoryConfig.model_validate({"path": "/r", "capability": ["a", "b"]})
        assert cfg.capabilities == ["a", "b"]

    def test_blank_capability_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentNodeRepositoryConfig.model_validate({"path": "/r", "capability": " "})


@pytest.mark.asyncio
class TestCapabilityContext:
    async def test_capability_prompt_precedes_user_prompt(self, env: Env) -> None:
        _use(
            env,
            _snapshot(CapabilityDefinition(capability_id="widgets", prompt="Widgets handbook.")),
            capability="widgets",
        )
        objective, metadata = await _objective(
            env, repo_full_name="acme/widgets", system_prompt="Be terse."
        )
        assert objective.startswith("--- Project context ---\nWidgets handbook.")
        ctx, system, user = (
            objective.index("Widgets handbook."),
            objective.index("Be terse."),
            objective.index("Fix the bug"),
        )
        assert ctx < system < user < objective.index("--- Environment ---")
        assert metadata["capabilities"] == ["widgets"]
        assert "do not modify the main checkout" in objective

    async def test_many_capabilities_and_nested_are_included_once(self, env: Env) -> None:
        _use(
            env,
            _snapshot(
                CapabilityDefinition(
                    capability_id="a", prompt="Prompt A.", nested_capabilities=["shared"]
                ),
                CapabilityDefinition(
                    capability_id="b", prompt="Prompt B.", nested_capabilities=["shared"]
                ),
                CapabilityDefinition(capability_id="shared", prompt="Shared prompt."),
                CapabilityDefinition(capability_id="empty", prompt="  "),
            ),
            capabilities=["a", "b", "empty"],
        )
        objective, metadata = await _objective(env, repo_full_name="acme/widgets")
        assert objective.count("Shared prompt.") == 1
        assert (
            objective.index("Prompt A.")
            < objective.index("Shared prompt.")
            < objective.index("Prompt B.")
        )
        assert metadata["capabilities"] == ["a", "shared", "b"]

    async def test_missing_capability_is_skipped(self, env: Env) -> None:
        _use(
            env,
            _snapshot(CapabilityDefinition(capability_id="widgets", prompt="Widgets handbook.")),
            capabilities=["ghost", "widgets"],
        )
        objective, metadata = await _objective(env, repo_full_name="acme/widgets")
        assert "Widgets handbook." in objective
        assert metadata["capabilities"] == ["widgets"]
        assert env.service.unknown_capabilities() == {"acme/widgets": ["ghost"]}

    async def test_live_snapshot_is_read_per_job(self, env: Env) -> None:
        current = {"s": _snapshot(CapabilityDefinition(capability_id="w", prompt="Old."))}
        _use(env, current["s"], capability="w")
        env.service._capability_snapshot = lambda: current["s"]
        first, _ = await _objective(env, key="k:1", repo_full_name="acme/widgets")
        current["s"] = _snapshot(CapabilityDefinition(capability_id="w", prompt="New."))
        second, _ = await _objective(env, key="k:2", repo_full_name="acme/widgets")
        assert "Old." in first and "New." in second

    async def test_job_without_repo_gets_no_capability_context(self, env: Env) -> None:
        _use(
            env,
            _snapshot(CapabilityDefinition(capability_id="widgets", prompt="Widgets handbook.")),
            capability="widgets",
        )
        objective, metadata = await _objective(env)
        assert "Project context" not in objective
        assert metadata["capabilities"] == []
