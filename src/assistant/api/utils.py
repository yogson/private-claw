import uuid
from typing import Literal

from assistant.channels.telegram import ActionButton, ChannelResponse, MessageType


def build_text_channel_response(
    text: str,
    session_id: str,
    trace_id: str,
    channel: str = "telegram",
    parse_mode: None | Literal["Markdown"] = None,
) -> ChannelResponse:
    return ChannelResponse(
        response_id=str(uuid.uuid4()),
        channel=channel,
        session_id=session_id,
        trace_id=trace_id,
        message_type=MessageType.TEXT,
        text=text,
        parse_mode=parse_mode,
    )


def build_webapp_button_channel_response(
    text: str,
    session_id: str,
    trace_id: str,
    buttons: list[dict[str, str]],
    channel: str = "telegram",
) -> ChannelResponse:
    """Build an interactive ChannelResponse with one or more WebApp reply-keyboard buttons.

    Uses reply_keyboard (KeyboardButton + WebAppInfo) instead of inline_keyboard so that
    Telegram.WebApp.sendData() works in the Mini App.  sendData() is only available when
    the Mini App is launched from a reply-keyboard button; it silently fails when opened
    from an inline-keyboard button.
    """
    actions: list[ActionButton] = [
        ActionButton(
            label=btn.get("label", "Open"),
            callback_id=btn.get("callback_id", ""),
            callback_data=btn.get("callback_data", ""),
            web_app_url=btn.get("web_app_url") or None,
        )
        for btn in buttons
    ]
    return ChannelResponse(
        response_id=str(uuid.uuid4()),
        channel=channel,
        session_id=session_id,
        trace_id=trace_id,
        message_type=MessageType.INTERACTIVE,
        text=text,
        ui_kind="reply_keyboard",
        actions=actions,
    )
