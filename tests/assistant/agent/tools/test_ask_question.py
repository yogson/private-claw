"""Tests for ask_question tool."""

from unittest.mock import MagicMock

from assistant.agent.tools.ask_question import ask_question
from assistant.agent.tools.deps import TurnDeps


def test_ask_question_returns_question_asked_with_options() -> None:
    """ask_question returns pending_user_selection with normalized options."""
    ctx = MagicMock()
    ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set(), memory_search_handler=None)

    result = ask_question(ctx, question="What do you prefer?", options=["A", "B", "C"])

    assert result["status"] == "question_asked"
    assert result["question"] == "What do you prefer?"
    assert result["options"] == [
        {"id": "0", "label": "A"},
        {"id": "1", "label": "B"},
        {"id": "2", "label": "C"},
    ]


def test_ask_question_rejects_empty_options() -> None:
    """ask_question returns rejected_invalid when options is empty."""
    ctx = MagicMock()
    ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set(), memory_search_handler=None)

    result = ask_question(ctx, question="Choose?", options=[])

    assert result["status"] == "rejected_invalid"
    assert "empty" in result["reason"].lower()


def test_ask_question_rejects_allow_multiple() -> None:
    """ask_question returns rejected_invalid when allow_multiple is True."""
    ctx = MagicMock()
    ctx.deps = TurnDeps(writes_approved=[], seen_intent_ids=set(), memory_search_handler=None)

    result = ask_question(ctx, question="Pick?", options=["X", "Y"], allow_multiple=True)

    assert result["status"] == "rejected_invalid"
    assert "allow_multiple" in result["reason"].lower()
