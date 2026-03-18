"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Ask question tool for closed questions with answer options.
Platform-agnostic: returns structured data; channel layer maps options to UI (e.g. buttons).
"""

import json
from typing import Annotated, Any

import structlog
from pydantic import BeforeValidator
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)

_QUESTION_ASKED_STATUS = "question_asked"
_USER_ANSWER_MESSAGE = (
    "Question was asked. The user's answer will be provided in their next message."
)
_MAX_OPTIONS = 10


def _coerce_options_list(v: list[str] | str) -> list[str]:
    """Coerce options from JSON string to list when LLM returns string instead of array."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
            return []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


OptionsList = Annotated[list[str], BeforeValidator(_coerce_options_list)]


def ask_question(
    ctx: RunContext[TurnDeps],
    question: str,
    options: OptionsList,
    allow_multiple: bool = False,
) -> dict[str, Any]:
    """Ask a closed question with answer options.

    Returns immediately; user answer comes as next message. Channel layer renders
    options as UI (e.g. buttons). v1 supports single-select only.
    """
    if not options:
        logger.info(
            "provider.tool_call.ask_question",
            status="rejected_invalid",
            reason="options cannot be empty",
        )
        return {
            "status": "rejected_invalid",
            "reason": "options cannot be empty",
        }
    bounded = options[:_MAX_OPTIONS]
    normalized = [
        {"id": str(i), "label": str(o).strip() or f"Option {i}"} for i, o in enumerate(bounded)
    ]
    if allow_multiple:
        logger.info(
            "provider.tool_call.ask_question",
            status="rejected_invalid",
            reason="allow_multiple not yet supported",
        )
        return {
            "status": "rejected_invalid",
            "reason": "allow_multiple is not yet supported; use single-select",
        }
    logger.info(
        "provider.tool_call.ask_question",
        status=_QUESTION_ASKED_STATUS,
        question=question[:80],
        option_count=len(normalized),
    )
    return {
        "status": _QUESTION_ASKED_STATUS,
        "message": _USER_ANSWER_MESSAGE,
        "question": (question or "").strip() or "Please choose an option.",
        "options": normalized,
    }
