"""Unit tests for _extract_pending_webapp_buttons."""

import json

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolReturnPart

from assistant.agent.webapp_button_extractor import _extract_pending_webapp_buttons


def _make_tool_return_part(
    tool_name: str,
    content: dict,
    tool_call_id: str = "call_1",
) -> ToolReturnPart:
    return ToolReturnPart(
        tool_name=tool_name,
        content=json.dumps(content),
        tool_call_id=tool_call_id,
    )


def _make_request_with_tool_return(
    tool_name: str,
    content: dict,
) -> ModelRequest:
    return ModelRequest(parts=[_make_tool_return_part(tool_name, content)])


class TestExtractPendingWebappButtons:
    def test_happy_path_returns_buttons_and_message(self) -> None:
        """Tool result with actions + web_app_url + message returns buttons tuple."""
        msg = _make_request_with_tool_return(
            tool_name="start_exercise",
            content={
                "status": "exercise_ready",
                "message": "Your exercise is ready!",
                "actions": [
                    {
                        "label": "🃏 Start",
                        "web_app_url": "https://example.com/exercise",
                        "callback_id": "start_exercise",
                        "callback_data": "",
                    }
                ],
            },
        )
        result = _extract_pending_webapp_buttons([msg])
        assert result is not None
        buttons, message = result
        assert len(buttons) == 1
        assert buttons[0]["label"] == "🃏 Start"
        assert buttons[0]["web_app_url"] == "https://example.com/exercise"
        assert buttons[0]["callback_id"] == "start_exercise"
        assert buttons[0]["callback_data"] == ""
        assert message == "Your exercise is ready!"

    def test_no_web_app_url_in_actions_returns_none(self) -> None:
        """Actions without web_app_url are ignored; returns None when none qualify."""
        msg = _make_request_with_tool_return(
            tool_name="start_exercise",
            content={
                "status": "exercise_ready",
                "actions": [
                    {
                        "label": "Something",
                        "callback_id": "foo",
                        "callback_data": "",
                        # no web_app_url
                    }
                ],
            },
        )
        result = _extract_pending_webapp_buttons([msg])
        assert result is None

    def test_no_actions_key_returns_none(self) -> None:
        """Tool result without an 'actions' key returns None."""
        msg = _make_request_with_tool_return(
            tool_name="start_exercise",
            content={"status": "no_words_due", "message": "Nothing to do."},
        )
        result = _extract_pending_webapp_buttons([msg])
        assert result is None

    def test_non_start_exercise_tool_is_ignored(self) -> None:
        """ToolReturnPart from a different tool is skipped even if it has web_app_url."""
        msg = _make_request_with_tool_return(
            tool_name="some_other_tool",
            content={
                "actions": [
                    {
                        "label": "Click",
                        "web_app_url": "https://example.com/other",
                        "callback_id": "other",
                        "callback_data": "",
                    }
                ],
            },
        )
        result = _extract_pending_webapp_buttons([msg])
        assert result is None

    def test_no_tool_return_part_in_response_returns_none(self) -> None:
        """ModelResponse messages (no ToolReturnPart) return None."""
        msg = ModelResponse(parts=[TextPart(content="Hello!")], model_name="claude-3")
        result = _extract_pending_webapp_buttons([msg])
        assert result is None

    def test_empty_message_list_returns_none(self) -> None:
        """Empty new_messages list returns None."""
        result = _extract_pending_webapp_buttons([])
        assert result is None

    def test_message_field_defaults_to_empty_string(self) -> None:
        """When tool result has no 'message' field, returned message is empty string."""
        msg = _make_request_with_tool_return(
            tool_name="start_exercise",
            content={
                "actions": [
                    {
                        "label": "Go",
                        "web_app_url": "https://example.com/exercise",
                        "callback_id": "start_exercise",
                        "callback_data": "",
                    }
                ],
            },
        )
        result = _extract_pending_webapp_buttons([msg])
        assert result is not None
        _, message = result
        assert message == ""
