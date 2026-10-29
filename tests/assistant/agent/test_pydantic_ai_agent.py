"""Tests for Pydantic AI turn adapter helpers."""

import base64
import json
import subprocess
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from assistant.agent.pydantic_ai_agent import (
    ToolCallFailedWithPartial,
    _extract_recovered_text,
    _last_tool_call_name,
    _llm_messages_to_history,
    _message_to_user_prompt_content,
    _new_messages_to_plans,
    _new_messages_to_session_records,
    _normalize_candidate_for_upsert,
)
from assistant.agent.tools import TurnDeps
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


def test_message_to_user_prompt_content_maps_multimodal_blocks() -> None:
    pdf_b64 = base64.b64encode(b"%PDF-1.7 mock bytes").decode("utf-8")
    prompt = _message_to_user_prompt_content(
        {
            "role": "user",
            "content_blocks": [
                {"type": "text", "text": "Summarize this file"},
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
            ],
        }
    )

    assert isinstance(prompt, list)
    assert prompt[0] == "Summarize this file"
    assert isinstance(prompt[1], BinaryContent)
    assert prompt[1].media_type == "application/pdf"
    assert prompt[1].data.startswith(b"%PDF")


def test_message_to_user_prompt_content_falls_back_to_text_on_invalid_blocks() -> None:
    prompt = _message_to_user_prompt_content(
        {
            "role": "user",
            "content": "fallback text",
            "content_blocks": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": "not-base64",
                    },
                }
            ],
        }
    )

    assert prompt == "fallback text"


def test_provider_module_imports_without_orchestrator_cycle() -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import assistant.agent.pydantic_ai_agent as m; "
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


def test_new_messages_to_session_records_preserves_args_sent_as_raw_json_string() -> None:
    """ToolCallPart.args often arrives as the raw JSON string the model streamed,
    not a pre-parsed dict (pydantic_ai's Anthropic responses commonly do this for
    non-trivial args). Persisting must not silently drop those args as "{}" —
    that corrupts replayed history into a false precedent of the tool "succeeding"
    with empty args, which the model then imitates on later turns."""
    now = datetime.now(UTC)
    records = _new_messages_to_session_records(
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="delegate_subagent_task",
                        tool_call_id="tc-1",
                        args='{"objective":"Review PR #464","backend":"claude_code"}',
                    ),
                ]
            ),
        ],
        session_id="s1",
        turn_id="t1",
        timestamp=now,
        assistant_msg_id="msg-t1-assistant",
    )
    tool_calls = [r for r in records if r.record_type == SessionRecordType.ASSISTANT_TOOL_CALL]
    assert len(tool_calls) == 1
    assert tool_calls[0].payload["arguments_json"] == (
        '{"objective":"Review PR #464","backend":"claude_code"}'
    )


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


class TestExtractRecoveredText:
    """Tests for _extract_recovered_text helper."""

    def test_extracts_last_text_from_model_responses(self) -> None:
        msgs = [
            ModelResponse(parts=[TextPart(content="First explanation.")]),
            ModelResponse(
                parts=[
                    TextPart(content="I will delegate the task."),
                    ToolCallPart(
                        tool_name="delegate_subagent_task",
                        tool_call_id="tc-1",
                        args={},
                    ),
                ]
            ),
        ]
        assert _extract_recovered_text(msgs) == "I will delegate the task."

    def test_returns_empty_string_when_no_text(self) -> None:
        msgs = [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="delegate_subagent_task",
                        tool_call_id="tc-1",
                        args={},
                    )
                ]
            ),
        ]
        assert _extract_recovered_text(msgs) == ""

    def test_returns_empty_for_empty_messages(self) -> None:
        assert _extract_recovered_text([]) == ""


class TestToolCallFailedWithPartialSubclass:
    """Verify ToolCallFailedWithPartial is a proper UnexpectedModelBehavior subclass."""

    def test_is_unexpected_model_behavior(self) -> None:
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        exc = ToolCallFailedWithPartial("test", partial_messages=[], recovered_text="hello")
        assert isinstance(exc, UnexpectedModelBehavior)

    def test_carries_recovered_text(self) -> None:
        exc = ToolCallFailedWithPartial("test", partial_messages=[], recovered_text="model words")
        assert exc.recovered_text == "model words"
        assert exc.partial_messages == []

    def test_body_forwarded_to_super(self) -> None:
        exc = ToolCallFailedWithPartial(
            "msg", partial_messages=[], recovered_text="", body="original error body"
        )
        assert exc.body == "original error body"


class TestLastToolCallName:
    """Tests for _last_tool_call_name helper."""

    def test_returns_last_tool_name(self) -> None:
        msgs = [
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="first_tool", tool_call_id="tc-1", args={}),
                ]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="second_tool", tool_call_id="tc-2", args={}),
                ]
            ),
        ]
        assert _last_tool_call_name(msgs) == "second_tool"

    def test_returns_none_for_no_tool_calls(self) -> None:
        msgs = [ModelResponse(parts=[TextPart(content="hello")])]
        assert _last_tool_call_name(msgs) is None

    def test_returns_none_for_empty(self) -> None:
        assert _last_tool_call_name([]) is None


class TestRunTurnExceptionWrapping:
    """Test the run_turn() path that wraps UnexpectedModelBehavior into ToolCallFailedWithPartial.

    This exercises the actual adapter code rather than mocking the adapter,
    verifying the exception transformation logic in PydanticAITurnAdapter.run_turn().
    """

    @pytest.mark.asyncio
    async def test_unexpected_model_behavior_wrapped_into_tool_call_failed(self) -> None:
        """When the agent iter raises UnexpectedModelBehavior, run_turn wraps it
        into ToolCallFailedWithPartial with the original chained as __cause__."""

        from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter

        original_cause = ValueError("empty tool args")
        umb = UnexpectedModelBehavior("tool call validation failed")
        umb.__cause__ = original_cause

        class _RaisingAsyncIter:
            """Async iterator that raises UMB on first __anext__."""

            def __aiter__(self) -> "_RaisingAsyncIter":
                return self

            async def __anext__(self) -> None:
                raise umb

        class _FakeAgentRun:
            """Minimal mock of pydantic_ai AgentRun async context manager."""

            async def __aenter__(self) -> "_FakeAgentRun":
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            def __aiter__(self) -> _RaisingAsyncIter:
                return _RaisingAsyncIter()

            def new_messages(self) -> list[object]:
                return []

        mock_agent = MagicMock()
        mock_agent.iter = MagicMock(return_value=_FakeAgentRun())

        adapter = PydanticAITurnAdapter.__new__(PydanticAITurnAdapter)
        adapter._model_id = "anthropic:test"
        adapter._max_tokens = 1024
        adapter._system_prompt = "test"
        adapter._agent = mock_agent

        deps = TurnDeps(writes_approved=[], seen_intent_ids=set())

        with pytest.raises(ToolCallFailedWithPartial) as exc_info:
            await adapter.run_turn(
                messages=[{"role": "user", "content": "hello"}],
                deps=deps,
                trace_id="test-trace",
            )

        wrapped = exc_info.value
        # The original UMB should be the __cause__
        assert wrapped.__cause__ is umb
        assert isinstance(wrapped, ToolCallFailedWithPartial)
        assert wrapped.partial_messages == []
        assert wrapped.recovered_text == ""


def _retried_then_ok_messages(tool_name: str) -> list[ModelMessage]:
    return [
        ModelResponse(parts=[ToolCallPart(tool_name=tool_name, tool_call_id="tc-r", args={})]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name=tool_name,
                    tool_call_id="tc-r",
                    content="Invalid arguments, please retry",
                    outcome="retried",
                )
            ]
        ),
    ]


def test_new_messages_to_session_records_skips_retried_tool_returns() -> None:
    """pydantic-ai v2 reports retried tool calls as ToolReturnPart(outcome='retried')."""
    records = _new_messages_to_session_records(
        _retried_then_ok_messages("some_tool"),
        session_id="s1",
        turn_id="t1",
        timestamp=datetime.now(UTC),
        assistant_msg_id="msg-t1-assistant",
    )
    assert [r for r in records if r.record_type == SessionRecordType.TOOL_RESULT] == []


def test_new_messages_to_plans_ignores_retried_memory_tool_returns() -> None:
    plans = _new_messages_to_plans(_retried_then_ok_messages("memory_propose_update"))
    # The retried return must not count as a result (same as RetryPromptPart before v2).
    assert [p.reason for p in plans] == ["missing tool result"]


@pytest.mark.asyncio
async def test_run_turn_reports_usage_end_to_end_with_real_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build adapter from the real config dir and run a turn on a FunctionModel."""
    from pathlib import Path

    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from assistant.agent.pydantic_ai_agent import PydanticAITurnAdapter
    from assistant.core.config.loader import ConfigLoader

    config_dir = Path(__file__).resolve().parents[3] / "config"
    # Dummy key: the Anthropic provider is built eagerly; no request is ever sent.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config = ConfigLoader(config_dir).load()

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="hello")])

    adapter = PydanticAITurnAdapter(
        model_id="anthropic:claude-3-5-sonnet-20241022", max_tokens=256, config=config
    )
    deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
    with adapter._agent.override(model=FunctionModel(respond)):
        text, new_msgs, usage = await adapter.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            deps=deps,
            trace_id="trace-1",
            model_id=None,
        )
    assert text == "hello"
    assert new_msgs
    assert usage is not None
    assert set(usage) == {"input_tokens", "output_tokens"}
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
