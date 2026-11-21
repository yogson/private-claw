"""Tests for the /wtf session recap service (issue #78)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from assistant.core.config.schemas import RecapConfig
from assistant.core.confirmation.history import CONFIRMATION_TURN_PREFIX
from assistant.core.orchestrator.persistence import FAILED_TURN_PLACEHOLDER
from assistant.core.orchestrator.recap import (
    OMITTED_MARKER,
    RECAP_PROMPT,
    SessionRecapService,
    build_recap_input,
    dialogue_lines,
)
from assistant.store.models import SessionRecord, SessionRecordType, SystemMessageScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rec(
    sequence: int,
    record_type: SessionRecordType,
    payload: dict[str, Any],
    turn_id: str = "t1",
    event_id: str | None = None,
) -> SessionRecord:
    return SessionRecord(
        session_id="s1",
        sequence=sequence,
        event_id=event_id or f"evt-{sequence}",
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        record_type=record_type,
        payload=payload,
    )


def _user(sequence: int, content: str, turn_id: str = "t1") -> SessionRecord:
    return _rec(
        sequence,
        SessionRecordType.USER_MESSAGE,
        {"message_id": f"m-{sequence}", "content": content},
        turn_id=turn_id,
    )


def _assistant(sequence: int, content: str, turn_id: str = "t1") -> SessionRecord:
    return _rec(
        sequence,
        SessionRecordType.ASSISTANT_MESSAGE,
        {"message_id": f"m-{sequence}", "content": content},
        turn_id=turn_id,
    )


def _tool_call(sequence: int) -> SessionRecord:
    return _rec(
        sequence,
        SessionRecordType.ASSISTANT_TOOL_CALL,
        {
            "message_id": f"m-{sequence}",
            "tool_call_id": "call-1",
            "tool_name": "secret_tool_name",
            "arguments_json": '{"secret_arg": "argvalue"}',
        },
    )


def _tool_result(sequence: int) -> SessionRecord:
    return _rec(
        sequence,
        SessionRecordType.TOOL_RESULT,
        {
            "message_id": f"m-{sequence}",
            "tool_call_id": "call-1",
            "tool_name": "secret_tool_name",
            "result": {"secret_output": "resultvalue"},
        },
    )


def _system(sequence: int) -> SessionRecord:
    return _rec(
        sequence,
        SessionRecordType.SYSTEM_MESSAGE,
        {
            "message_id": f"sys-{sequence}",
            "content": "SYSTEM PROMPT TEXT",
            "scope": SystemMessageScope.SESSION.value,
        },
        turn_id="t0",
    )


def _terminal(sequence: int, turn_id: str = "t1") -> SessionRecord:
    return _rec(sequence, SessionRecordType.TURN_TERMINAL, {"status": "completed"}, turn_id=turn_id)


def _turn_summary(sequence: int) -> SessionRecord:
    return _rec(sequence, SessionRecordType.TURN_SUMMARY, {"summary_text": "TURN SUMMARY TEXT"})


def _compaction_summary(sequence: int, content: str) -> SessionRecord:
    return _rec(
        sequence,
        SessionRecordType.COMPACTION_SUMMARY,
        {
            "message_id": f"summary-{sequence}",
            "content": content,
            "scope": SystemMessageScope.SESSION.value,
        },
        turn_id="compaction-2024-01-01T00:00:00",
        event_id=f"compaction-{sequence}",
    )


def _confirmation_resolution(sequence: int) -> SessionRecord:
    turn_id = f"{CONFIRMATION_TURN_PREFIX}abc123"
    return _rec(
        sequence,
        SessionRecordType.USER_MESSAGE,
        {"message_id": f"m-{sequence}", "content": "CONFIRMATION RESOLUTION TEXT"},
        turn_id=turn_id,
        event_id=turn_id,
    )


def _mixed_session() -> list[SessionRecord]:
    return [
        _system(0),
        _compaction_summary(1, "We set up the Telegram bot earlier."),
        _user(2, "Please add the /wtf command"),
        _tool_call(3),
        _tool_result(4),
        _assistant(5, "Added the command to the enum"),
        _turn_summary(6),
        _terminal(7),
        _confirmation_resolution(8),
        _user(9, "   ", turn_id="t2"),
        _assistant(10, FAILED_TURN_PLACEHOLDER, turn_id="t2"),
        _terminal(11, turn_id="t2"),
    ]


class _CapturingModel:
    """FunctionModel stub that records the prompt and settings it was called with."""

    def __init__(self, output: str = "🎯 Goal\n✅ Done:\n• thing") -> None:
        self.output = output
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.model = FunctionModel(self._respond)

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = ""
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                        prompt = part.content
        self.calls.append((prompt, dict(info.model_settings or {})))
        return ModelResponse(parts=[TextPart(self.output)])


def _failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise RuntimeError("LLM exploded")


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestDialogueFiltering:
    def test_keeps_only_user_and_assistant_text(self) -> None:
        assert dialogue_lines(_mixed_session()) == [
            ("USER", "Please add the /wtf command"),
            ("ASSISTANT", "Added the command to the enum"),
        ]

    def test_orders_by_sequence(self) -> None:
        records = [_assistant(2, "second"), _user(1, "first")]
        assert dialogue_lines(records) == [("USER", "first"), ("ASSISTANT", "second")]

    def test_recap_input_has_no_tool_or_system_leakage(self) -> None:
        text = build_recap_input(_mixed_session(), max_input_chars=10_000)
        assert text is not None
        assert "USER: Please add the /wtf command" in text
        assert "ASSISTANT: Added the command to the enum" in text
        for leaked in (
            "secret_tool_name",
            "secret_arg",
            "argvalue",
            "secret_output",
            "resultvalue",
            "Tool call",
            "Tool result",
            "SYSTEM PROMPT TEXT",
            "TURN SUMMARY TEXT",
            "CONFIRMATION RESOLUTION TEXT",
            "completed",
            FAILED_TURN_PLACEHOLDER,
        ):
            assert leaked not in text, leaked

    def test_compaction_summary_included_as_earlier_context(self) -> None:
        text = build_recap_input(_mixed_session(), max_input_chars=10_000)
        assert text is not None
        assert text.startswith("Earlier context:\nWe set up the Telegram bot earlier.")
        assert text.index("Earlier context:") < text.index("USER: Please add")

    def test_multiple_compaction_summaries_oldest_first(self) -> None:
        records = [
            _compaction_summary(0, "first pass"),
            _compaction_summary(1, "second pass"),
            _user(2, "hi"),
        ]
        text = build_recap_input(records, max_input_chars=10_000)
        assert text is not None
        assert text.index("first pass") < text.index("second pass") < text.index("USER: hi")

    def test_compaction_summary_alone_is_recappable(self) -> None:
        text = build_recap_input([_compaction_summary(0, "old stuff")], max_input_chars=10_000)
        assert text == "Earlier context:\nold stuff"


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    def test_keeps_most_recent_messages_and_marks_omission(self) -> None:
        records = [_user(i, f"message-{i:02d} " + "x" * 80) for i in range(20)]
        text = build_recap_input(records, max_input_chars=1_000)
        assert text is not None
        assert len(text) <= 1_100  # budget plus the small headers
        assert OMITTED_MARKER in text
        assert "message-19" in text
        assert "message-00" not in text
        kept = [i for i in range(20) if f"message-{i:02d}" in text]
        assert kept == list(range(kept[0], 20))  # a contiguous most-recent tail

    def test_no_marker_when_everything_fits(self) -> None:
        text = build_recap_input([_user(0, "short"), _assistant(1, "reply")], 10_000)
        assert text is not None
        assert OMITTED_MARKER not in text

    def test_oversized_single_message_is_truncated_not_dropped(self) -> None:
        records = [_user(0, "old"), _assistant(1, "HEAD" + "y" * 5_000)]
        text = build_recap_input(records, max_input_chars=1_000)
        assert text is not None
        assert "ASSISTANT: HEAD" in text
        assert "USER: old" not in text
        assert OMITTED_MARKER in text
        assert len(text) <= 1_100

    def test_long_compaction_summary_is_capped(self) -> None:
        records = [_compaction_summary(0, "S" * 5_000 + "NEWEST"), _user(1, "latest question")]
        text = build_recap_input(records, max_input_chars=1_500)
        assert text is not None
        assert "NEWEST" in text  # the newest end of the summary survives
        assert "USER: latest question" in text
        assert len(text) <= 1_600


# ---------------------------------------------------------------------------
# Empty sessions
# ---------------------------------------------------------------------------


class TestEmptySession:
    @pytest.mark.parametrize(
        "records",
        [
            [],
            [_system(0), _tool_call(1), _tool_result(2), _terminal(3)],
            [_user(0, "  "), _assistant(1, FAILED_TURN_PLACEHOLDER), _confirmation_resolution(2)],
        ],
        ids=["no-records", "tool-only", "blank-and-synthetic"],
    )
    async def test_returns_none_without_llm_call(self, records: list[SessionRecord]) -> None:
        assert build_recap_input(records, 10_000) is None
        svc = SessionRecapService(model_id="claude-haiku-4-5")
        stub = _CapturingModel()
        with svc._agent.override(model=stub.model):  # noqa: SLF001
            assert await svc.recap(records, "trace-empty") is None
        assert stub.calls == []


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TestSessionRecapService:
    async def test_recap_calls_llm_with_dialogue_and_max_tokens(self) -> None:
        svc = SessionRecapService(model_id="claude-haiku-4-5", max_tokens=321)
        stub = _CapturingModel(output="  🎯 Adding /wtf\n✅ Done:\n• enum  \n")
        with svc._agent.override(model=stub.model):  # noqa: SLF001
            recap = await svc.recap(_mixed_session(), "trace-1")
        assert recap == "🎯 Adding /wtf\n✅ Done:\n• enum"
        assert len(stub.calls) == 1
        prompt, settings = stub.calls[0]
        assert "USER: Please add the /wtf command" in prompt
        assert "secret_tool_name" not in prompt
        assert settings.get("max_tokens") == 321

    async def test_llm_failure_propagates(self) -> None:
        svc = SessionRecapService(model_id="claude-haiku-4-5")
        with (
            svc._agent.override(model=FunctionModel(_failing_model)),  # noqa: SLF001
            pytest.raises(RuntimeError, match="LLM exploded"),
        ):
            await svc.recap([_user(0, "hello")], "trace-fail")

    def test_construction_does_not_require_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert SessionRecapService(model_id="claude-haiku-4-5") is not None

    def test_default_prompt_requests_structure_and_language(self) -> None:
        markers = (
            "🎯",
            "📍",
            "✅ Done:",
            "⏭ Next:",
            "2–4 sentences",
            "up to 5",
            "up to 3",
            "same language as the conversation",
            "Earlier context",
            "nothing remains",
            "Output only the recap.",
        )
        for marker in markers:
            assert marker in RECAP_PROMPT

    @pytest.mark.parametrize("prompt", ["", "   "])
    def test_empty_prompt_uses_default(self, prompt: str) -> None:
        svc = SessionRecapService(model_id="claude-haiku-4-5", prompt=prompt)
        assert svc._agent._system_prompts == (RECAP_PROMPT,)  # noqa: SLF001

    def test_custom_prompt_overrides_default(self) -> None:
        custom = "Recap in one line."
        svc = SessionRecapService(model_id="claude-haiku-4-5", prompt=custom)
        assert svc._agent._system_prompts == (custom,)  # noqa: SLF001


class TestRecapConfig:
    def test_defaults(self) -> None:
        cfg = RecapConfig()
        assert cfg.enabled is True
        assert cfg.model_id == "claude-haiku-4-5"
        assert cfg.max_tokens == 512
        assert cfg.max_input_chars == 30_000
        assert cfg.prompt == ""

    def test_rejects_tiny_budgets(self) -> None:
        with pytest.raises(ValueError):
            RecapConfig(max_tokens=1)
        with pytest.raises(ValueError):
            RecapConfig(max_input_chars=10)
