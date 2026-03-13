"""Tests for Pydantic AI turn adapter helpers."""

import json
import subprocess
import sys
from datetime import UTC, datetime

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from assistant.providers.pydantic_ai_agent import (
    _llm_messages_to_history,
    _new_messages_to_plans,
    _new_messages_to_session_records,
    _normalize_candidate_for_upsert,
)
from assistant.store.models import SessionRecordType


def test_llm_messages_to_history_preserves_tool_use_and_tool_result() -> None:
    """Replay context with tool calls must convert to ModelRequest/ModelResponse correctly."""
    history = _llm_messages_to_history(
        [
            {"role": "user", "content": "What is my name?"},
            {
                "role": "assistant",
                "content": "",
                "content_blocks": [
                    {
                        "type": "tool_use",
                        "id": "tc-1",
                        "name": "memory_search",
                        "input": {"query": "user name", "limit": 3},
                    }
                ],
            },
            {
                "role": "user",
                "content": "",
                "content_blocks": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tc-1",
                        "tool_name": "memory_search",
                        "content": '{"status":"ok","matches":[{"body":"Egor"}]}',
                        "is_error": False,
                    }
                ],
            },
            {"role": "assistant", "content": "Your name is Egor!"},
        ]
    )
    assert len(history) == 4
    assert isinstance(history[0], ModelRequest)
    assert isinstance(history[1], ModelResponse)
    assert isinstance(history[2], ModelRequest)
    assert isinstance(history[3], ModelResponse)
    # Second message should have ToolCallPart
    resp1 = history[1]
    assert len(resp1.parts) == 1
    assert isinstance(resp1.parts[0], ToolCallPart)
    assert resp1.parts[0].tool_name == "memory_search"
    # Third message should have ToolReturnPart
    req2 = history[2]
    assert len(req2.parts) == 1
    assert isinstance(req2.parts[0], ToolReturnPart)
    assert req2.parts[0].tool_call_id == "tc-1"


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


def test_new_messages_to_session_records_persists_all_tool_calls_and_results() -> None:
    """Verify memory_search and other tool calls/results are converted for replay."""
    now = datetime.now(UTC)
    records = _new_messages_to_session_records(
        [
            ModelResponse(
                parts=[
                    TextPart(content="Let me search memory."),
                    ToolCallPart(
                        tool_name="memory_search",
                        tool_call_id="tc-search-1",
                        args={"query": "user name", "limit": 3, "memory_types": ["profile"]},
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="memory_search",
                        tool_call_id="tc-search-1",
                        content='{"status":"ok","matches":[{"body":"Name: Egor"}]}',
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content="Your name is Egor!")]),
        ],
        session_id="s1",
        turn_id="t1",
        timestamp=now,
        assistant_msg_id="msg-t1-assistant",
        model_id="claude-3-5",
        skip_memory_tool_results=True,
    )
    types = [r.record_type for r in records]
    assert SessionRecordType.ASSISTANT_MESSAGE in types
    assert SessionRecordType.ASSISTANT_TOOL_CALL in types
    assert SessionRecordType.TOOL_RESULT in types
    tool_calls = [r for r in records if r.record_type == SessionRecordType.ASSISTANT_TOOL_CALL]
    tool_results = [r for r in records if r.record_type == SessionRecordType.TOOL_RESULT]
    assert len(tool_calls) == 1
    assert tool_calls[0].payload["tool_name"] == "memory_search"
    assert (
        tool_calls[0].payload["arguments_json"]
        == '{"query":"user name","limit":3,"memory_types":["profile"]}'
    )
    assert len(tool_results) == 1
    assert tool_results[0].payload["tool_name"] == "memory_search"
    assert tool_results[0].payload["result"]["status"] == "ok"
    assert "Egor" in str(tool_results[0].payload["result"]["matches"])


def test_new_messages_to_session_records_skips_memory_propose_update_results() -> None:
    """memory_propose_update results are omitted when skip_memory_tool_results=True."""
    now = datetime.now(UTC)
    records = _new_messages_to_session_records(
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="memory_propose_update",
                        tool_call_id="tc-mem-1",
                        args={"intent_id": "x", "action": "upsert", "memory_type": "profile"},
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="memory_propose_update",
                        tool_call_id="tc-mem-1",
                        content='{"status":"approved_pending_apply"}',
                    )
                ]
            ),
        ],
        session_id="s1",
        turn_id="t1",
        timestamp=now,
        assistant_msg_id="msg-t1-assistant",
        skip_memory_tool_results=True,
    )
    tool_calls = [r for r in records if r.record_type == SessionRecordType.ASSISTANT_TOOL_CALL]
    tool_results = [r for r in records if r.record_type == SessionRecordType.TOOL_RESULT]
    assert len(tool_calls) == 1
    assert len(tool_results) == 0


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
