"""
Session record construction from pydantic_ai new_messages.

Converts ModelMessage objects into SessionRecord instances for full
conversation replay and restore.
"""

import json
from datetime import datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from assistant.agent.constants import MEMORY_TOOL_NAME
from assistant.agent.message_converters import _parse_tool_result_content
from assistant.store.models import (
    AssistantToolCallPayload,
    SessionRecord,
    SessionRecordType,
    ToolResultPayload,
)


def _new_messages_to_session_records(
    new_messages: list[ModelMessage],
    *,
    session_id: str,
    turn_id: str,
    timestamp: datetime,
    assistant_msg_id: str,
    model_id: str | None = None,
    usage: dict[str, int] | None = None,
    user_id: str | None = None,
    skip_memory_tool_results: bool = True,
) -> list[SessionRecord]:
    """Convert pydantic_ai new_messages to session records for full replay/restore.

    Emits ASSISTANT_MESSAGE, ASSISTANT_TOOL_CALL, and TOOL_RESULT records in
    chronological order. When skip_memory_tool_results is True, memory_propose_update
    tool results are omitted (caller persists them separately with applied outcomes).
    """
    records: list[SessionRecord] = []
    assistant_idx = 0

    for msg in new_messages:
        if isinstance(msg, ModelResponse):
            text_parts = [p for p in msg.parts if isinstance(p, TextPart)]
            tool_call_parts = [p for p in msg.parts if isinstance(p, ToolCallPart)]

            if text_parts:
                content = " ".join(p.content for p in text_parts if p.content).strip()
                msg_id = (
                    assistant_msg_id
                    if assistant_idx == 0
                    else f"{assistant_msg_id}-{assistant_idx}"
                )
                payload: dict[str, Any] = {
                    "message_id": msg_id,
                    "content": content or "",
                }
                if model_id:
                    payload["model_id"] = model_id
                if usage is not None and assistant_idx == 0:
                    payload["usage"] = usage
                    if user_id is not None:
                        payload["user_id"] = user_id
                records.append(
                    SessionRecord(
                        session_id=session_id,
                        sequence=0,
                        event_id=msg_id,
                        turn_id=turn_id,
                        timestamp=timestamp,
                        record_type=SessionRecordType.ASSISTANT_MESSAGE,
                        payload=payload,
                    )
                )
                assistant_idx += 1

            for part in tool_call_parts:
                args = part.args if isinstance(part.args, dict) else {}
                args_json = json.dumps(args, separators=(",", ":"))
                call_payload = AssistantToolCallPayload(
                    message_id=f"msg-{part.tool_call_id}",
                    tool_call_id=part.tool_call_id,
                    tool_name=part.tool_name,
                    arguments_json=args_json,
                )
                records.append(
                    SessionRecord(
                        session_id=session_id,
                        sequence=0,
                        event_id=f"assistant-tool-call-{part.tool_call_id}",
                        turn_id=turn_id,
                        timestamp=timestamp,
                        record_type=SessionRecordType.ASSISTANT_TOOL_CALL,
                        payload=call_payload.model_dump(),
                    )
                )

        elif isinstance(msg, ModelRequest):
            for req_part in msg.parts:
                if not isinstance(req_part, ToolReturnPart):
                    continue
                if skip_memory_tool_results and req_part.tool_name == MEMORY_TOOL_NAME:
                    continue
                result = _parse_tool_result_content(req_part.content)
                result_payload = ToolResultPayload(
                    message_id=f"msg-result-{req_part.tool_call_id}",
                    tool_call_id=req_part.tool_call_id,
                    tool_name=req_part.tool_name,
                    result=result,
                    error=None,
                )
                records.append(
                    SessionRecord(
                        session_id=session_id,
                        sequence=0,
                        event_id=f"tool-result-{req_part.tool_call_id}",
                        turn_id=turn_id,
                        timestamp=timestamp,
                        record_type=SessionRecordType.TOOL_RESULT,
                        payload=result_payload.model_dump(),
                    )
                )

    return records
