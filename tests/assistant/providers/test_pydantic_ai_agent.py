"""Tests for Pydantic AI turn adapter helpers."""

import json
import subprocess
import sys

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from assistant.providers.pydantic_ai_agent import (
    _llm_messages_to_history,
    _new_messages_to_plans,
    _normalize_candidate_for_upsert,
)


def test_llm_messages_to_history_wraps_user_parts_in_model_request() -> None:
    history = _llm_messages_to_history(
        [
            {"role": "user", "content": "remember this"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content_blocks": [{"type": "text", "text": "follow up"}]},
        ]
    )

    assert len(history) == 3
    assert isinstance(history[0], ModelRequest)
    assert isinstance(history[1], ModelResponse)
    assert isinstance(history[2], ModelRequest)
    assert isinstance(history[0].parts[0], UserPromptPart)
    assert history[0].parts[0].content == "remember this"


def test_provider_module_imports_without_orchestrator_cycle() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import assistant.providers.pydantic_ai_agent as m; "
            "assert hasattr(m, 'PydanticAITurnAdapter')"
        ),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_new_messages_to_plans_accepts_canonical_memory_tool_name() -> None:
    plans = _new_messages_to_plans(
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="memory_propose_update",
                        tool_call_id="tool-1",
                        args={
                            "intent_id": "intent-1",
                            "action": "upsert",
                            "memory_type": "profile",
                            "candidate": {
                                "tags": ["identity"],
                                "entities": ["Egor"],
                                "priority": 8,
                                "confidence": 0.9,
                                "body_markdown": "Name: Egor",
                            },
                            "reason": "explicit request",
                            "source": "explicit_user_request",
                            "requires_user_confirmation": False,
                        },
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="memory_propose_update",
                        tool_call_id="tool-1",
                        content='{"status":"approved_pending_apply","reason":"","requires_user_confirmation":false}',
                    )
                ]
            ),
        ]
    )
    assert len(plans) == 1
    assert plans[0].tool_call_id == "tool-1"
    assert plans[0].precheck_status == "approved_pending_apply"
    assert plans[0].intent is not None
    assert plans[0].intent.intent_id == "intent-1:tool-1"


def test_normalize_candidate_for_upsert_builds_body_and_entities_from_name() -> None:
    normalized = _normalize_candidate_for_upsert({"name": "Egor"})
    assert normalized["body_markdown"] == "- name: Egor"
    assert normalized["entities"] == ["Egor"]
    assert normalized["tags"] == ["user_profile"]


def test_new_messages_to_plans_normalizes_upsert_candidate_before_persist() -> None:
    plans = _new_messages_to_plans(
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="memory_propose_update",
                        tool_call_id="tool-2",
                        args={
                            "intent_id": "save_user_name",
                            "action": "upsert",
                            "memory_type": "profile",
                            "reason": "remember name",
                            "source": "explicit_user_request",
                            "requires_user_confirmation": False,
                            "candidate": {"name": "Egor"},
                        },
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="memory_propose_update",
                        tool_call_id="tool-2",
                        content='{"status":"pending_confirmation","reason":"requires_user_confirmation=true","requires_user_confirmation":true}',
                    )
                ]
            ),
        ]
    )
    assert len(plans) == 1
    args = json.loads(plans[0].intent_json)
    assert args["requires_user_confirmation"] is True
    assert args["candidate"]["body_markdown"] == "- name: Egor"
    assert args["candidate"]["entities"] == ["Egor"]
