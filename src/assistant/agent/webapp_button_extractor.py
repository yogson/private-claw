"""
WebApp button extraction from pydantic_ai new_messages.

Scans new messages for tool results that include WebApp button actions and
returns them so the orchestrator can render an inline WebApp keyboard button.
"""

from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

from assistant.agent.message_converters import _parse_tool_result_content


def _extract_pending_webapp_buttons(
    new_messages: list[ModelMessage],
) -> tuple[list[dict[str, str]], str] | None:
    """Extract WebApp button actions and message text from new_messages if present.

    Scans all ToolReturnPart entries in new_messages and collects any
    ``actions`` entries that carry a ``web_app_url`` field.  Returns a tuple
    of (buttons, message) for the first non-empty list found, where message is
    the ``message`` field from the tool result (empty string if absent).
    Returns None when no WebApp actions are present.
    """
    for msg in new_messages:
        if not isinstance(msg, ModelRequest):
            continue
        for req_part in msg.parts:
            if not isinstance(req_part, ToolReturnPart):
                continue
            result: dict[str, Any] = _parse_tool_result_content(req_part.content)
            actions = result.get("actions")
            if not isinstance(actions, list):
                continue
            webapp_buttons: list[dict[str, str]] = [
                {
                    "label": str(a.get("label", "")),
                    "web_app_url": str(a["web_app_url"]),
                    "callback_id": str(a.get("callback_id", "")),
                    "callback_data": str(a.get("callback_data", "")),
                }
                for a in actions
                if isinstance(a, dict) and a.get("web_app_url")
            ]
            if webapp_buttons:
                message = str(result.get("message", ""))
                return webapp_buttons, message
    return None
