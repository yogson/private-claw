import uuid
from typing import Literal

from assistant.channels.telegram import ChannelResponse, MessageType


def build_text_channel_response(
        text: str,
        session_id: str,
        trace_id: str,
        channel="telegram",
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
