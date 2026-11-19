"""Shared test helpers for confirmation history scenarios."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from assistant.agent.session_record_builder import _new_messages_to_session_records
from assistant.core.orchestrator.persistence import persist_turn_initial, persist_turn_terminal
from assistant.store.interfaces import SessionStoreInterface


async def persist_pending_proposal(
    store: SessionStoreInterface,
    session_id: str,
    confirmation_id: str,
    *,
    tool_name: str = "memory_propose_update",
    turn_id: str | None = None,
) -> None:
    """Persist a completed turn whose ``tool_name`` result says ``pending_confirmation``.

    The default tool is memory_propose_update, whose result is still pending.
    """
    turn_id = turn_id or f"proposal-{confirmation_id}"
    tool_call_id = f"tc-{turn_id}"
    new_msgs = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    args={"intent_id": "i", "action": "upsert", "memory_type": "facts"},
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    content=json.dumps(
                        {"status": "pending_confirmation", "confirmation_id": confirmation_id}
                    ),
                )
            ]
        ),
    ]
    records = _new_messages_to_session_records(
        new_msgs,
        session_id=session_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        assistant_msg_id=f"msg-{turn_id}-assistant",
    )
    config = SimpleNamespace(model=SimpleNamespace(default_model_id="m"))
    await persist_turn_initial(
        store,
        config,  # type: ignore[arg-type]
        session_id=session_id,
        turn_id=turn_id,
        user_text="remember I like tea",
        assistant_records=records,
    )
    await persist_turn_terminal(store, session_id=session_id, turn_id=turn_id)
