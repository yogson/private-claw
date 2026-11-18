"""Integration: confirmation proposals and resolutions are visible in replayed history."""

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from assistant.agent.session_record_builder import _new_messages_to_session_records
from assistant.core.confirmation import (
    CONFIRMATION_RESOLVED_MARKER,
    ConfirmationDecision,
    ConfirmationDraft,
    ConfirmationHandlerRegistry,
    ConfirmationService,
    SessionConfirmationRecorder,
)
from assistant.core.orchestrator.payloads import records_to_messages
from assistant.core.orchestrator.persistence import persist_turn_initial, persist_turn_terminal
from assistant.store.filesystem.session import FilesystemSessionStore

SESSION = "tg:7"


def _runtime_config() -> object:
    class _Model:
        default_model_id = "m"

    class _Cfg:
        model = _Model()

    return _Cfg()


async def _persist_proposal_turn(store: FilesystemSessionStore, confirmation_id: str) -> None:
    new_msgs = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="memory_propose_update",
                    tool_call_id="tc-mem",
                    args={"intent_id": "i", "action": "upsert", "memory_type": "facts"},
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="memory_propose_update",
                    tool_call_id="tc-mem",
                    content=json.dumps(
                        {"status": "pending_confirmation", "confirmation_id": confirmation_id}
                    ),
                )
            ]
        ),
    ]
    records = _new_messages_to_session_records(
        new_msgs,
        session_id=SESSION,
        turn_id="t1",
        timestamp=datetime.now(UTC),
        assistant_msg_id="msg-t1-assistant",
    )
    await persist_turn_initial(
        store,
        _runtime_config(),  # type: ignore[arg-type]
        session_id=SESSION,
        turn_id="t1",
        user_text="remember I like tea",
        assistant_records=records,
    )
    await persist_turn_terminal(store, session_id=SESSION, turn_id="t1")


async def test_replay_contains_proposal_and_resolution_record(tmp_path: Path) -> None:
    store = FilesystemSessionStore(tmp_path)
    service = ConfirmationService(
        ConfirmationHandlerRegistry(), recorder=SessionConfirmationRecorder(store)
    )
    draft = ConfirmationDraft(
        kind="memory_update", payload={}, summary_for_user="I like tea", dedupe_key="k"
    )
    request = (await service.request(SESSION, draft)).request
    assert request is not None
    await _persist_proposal_turn(store, request.confirmation_id)

    await service.resolve(request.confirmation_id, ConfirmationDecision.REJECT)

    messages = records_to_messages(await store.replay_for_turn(SESSION, 50))
    blocks = [b for m in messages for b in (m.content_blocks or [])]
    assert any(
        b.get("type") == "tool_use" and b.get("name") == "memory_propose_update" for b in blocks
    )
    assert any(
        b.get("type") == "tool_result" and request.confirmation_id in b.get("content", "")
        for b in blocks
    )
    resolution = [m.content for m in messages if m.content.startswith(CONFIRMATION_RESOLVED_MARKER)]
    assert len(resolution) == 1
    payload = json.loads(resolution[0].removeprefix(CONFIRMATION_RESOLVED_MARKER))
    assert payload["confirmation_id"] == request.confirmation_id
    assert payload["status"] == "rejected"
    assert payload["summary"] == "I like tea"
