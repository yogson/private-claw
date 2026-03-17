"""
Ask-question extraction from pydantic_ai new_messages.

Scans new messages for pending ask_question tool results and constructs
a PendingAskData object if one is found.
"""

from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

from assistant.agent.constants import ASK_QUESTION_TOOL_NAME
from assistant.agent.message_converters import _parse_tool_result_content
from assistant.core.orchestrator.models import PendingAskData


def _extract_pending_ask_question(
    new_messages: list[ModelMessage],
    *,
    session_id: str,
    turn_id: str,
) -> PendingAskData | None:
    """Extract pending ask_question from new_messages if present."""
    tool_results: dict[str, dict[str, Any]] = {}
    for msg in new_messages:
        if isinstance(msg, ModelRequest):
            for req_part in msg.parts:
                if (
                    isinstance(req_part, ToolReturnPart)
                    and req_part.tool_name == ASK_QUESTION_TOOL_NAME
                ):
                    tool_results[req_part.tool_call_id] = _parse_tool_result_content(
                        req_part.content
                    )
    for tool_call_id, result in tool_results.items():
        if result.get("status") == "question_asked":
            question = result.get("question", "Please choose an option.")
            options = result.get("options", [])
            if not isinstance(options, list):
                options = []
            return PendingAskData(
                question_id=tool_call_id,
                question=str(question),
                options=[
                    {"id": str(o.get("id", i)), "label": str(o.get("label", ""))}
                    for i, o in enumerate(options)
                    if isinstance(o, dict)
                ],
                session_id=session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
    return None
