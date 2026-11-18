"""
Integration tests for the Telegram confirmation flow: rendering after turns and
Approve/Reject callback handling (CMP_API_FASTAPI_GATEWAY + CMP_CHANNEL_TELEGRAM_ADAPTER).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.api.confirmation_handler import deliver_with_confirmations
from assistant.api.delegation_feedback_handler import _build_delegation_feedback_handler
from assistant.api.orchestrator_handler import _build_orchestrator_handler
from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.confirmation_render import NO_LONGER_VALID_TEXT
from assistant.channels.telegram.models import (
    CallbackQueryMeta,
    EventSource,
    EventType,
    MessageType,
    NormalizedEvent,
)
from assistant.core.config.schemas import TelegramChannelConfig
from assistant.core.confirmation import (
    CONFIRMATION_RESOLVED_MARKER,
    ConfirmationDraft,
    ConfirmationHandlerRegistry,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationService,
    ConfirmationStatus,
    SessionConfirmationRecorder,
)
from assistant.core.orchestrator.models import OrchestratorResult, PendingAskData
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.models import SessionRecordType, TaskRecord, TaskStatus

CHAT_ID = 123456
SESSION = f"tg:{CHAT_ID}"
MESSAGE_ID = 42


class _Handler:
    kind = "memory_update"

    def __init__(self) -> None:
        self.calls = 0

    async def apply(
        self, request: ConfirmationRequest, *, user_id: str | None
    ) -> ConfirmationOutcome:
        self.calls += 1
        return ConfirmationOutcome(ok=True, status="written", detail="written")


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(
        TelegramChannelConfig(enabled=True, bot_token="12345:test-token", allowlist=[CHAT_ID])
    )
    adapter.send_response = AsyncMock(return_value=True)  # type: ignore[method-assign]
    adapter.acknowledge_callback = AsyncMock()  # type: ignore[method-assign]
    adapter.edit_message_text = AsyncMock()  # type: ignore[method-assign]
    return adapter


def _service(handler: _Handler | None = None) -> ConfirmationService:
    return ConfirmationService(ConfirmationHandlerRegistry([handler or _Handler()]))


async def _pending(service: ConfirmationService) -> ConfirmationRequest:
    draft = ConfirmationDraft(
        kind="memory_update",
        payload={},
        summary_for_user="Action: upsert\nType: preferences\n\nUser prefers tea.",
        dedupe_key="k",
    )
    request = (await service.request(SESSION, draft)).request
    assert request is not None
    return request


def _button_data(adapter: TelegramAdapter, request: ConfirmationRequest, index: int) -> str:
    response = adapter.build_confirmation_response(chat_id=CHAT_ID, trace_id="t", request=request)
    return response.actions[index].callback_data


def _callback_event(data: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-cb",
        event_type=EventType.USER_CALLBACK_QUERY,
        source=EventSource.TELEGRAM,
        session_id=SESSION,
        user_id=str(CHAT_ID),
        created_at=datetime.now(UTC),
        trace_id="trace-cb",
        callback_query=CallbackQueryMeta(
            callback_id="cq-1", callback_data=data, origin_message_id=MESSAGE_ID
        ),
        metadata={"chat_id": CHAT_ID, "origin_message_text": "🧠 Save this to memory?\n\nold"},
    )


def _text_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_id="ev-text",
        event_type=EventType.USER_TEXT_MESSAGE,
        source=EventSource.TELEGRAM,
        session_id=SESSION,
        user_id=str(CHAT_ID),
        created_at=datetime.now(UTC),
        trace_id="trace-text",
        text="remember I like tea",
        metadata={"chat_id": CHAT_ID},
    )


def _orchestrator(result: OrchestratorResult | None = None) -> MagicMock:
    orchestrator = MagicMock()
    orchestrator.execute_turn = AsyncMock(return_value=result)
    return orchestrator


async def _press(adapter: TelegramAdapter, service: ConfirmationService, data: str) -> MagicMock:
    orchestrator = _orchestrator()
    handler = _build_orchestrator_handler(adapter, orchestrator, confirmations=service)
    assert await handler(_callback_event(data)) is None
    orchestrator.execute_turn.assert_not_awaited()
    return orchestrator


def _ack_text(adapter: TelegramAdapter) -> str:
    return adapter.acknowledge_callback.await_args.args[1]  # type: ignore[attr-defined]


async def test_approve_saves_answers_and_edits_original_message() -> None:
    adapter, handler = _adapter(), _Handler()
    service = _service(handler)
    request = await _pending(service)

    await _press(adapter, service, _button_data(adapter, request, 0))

    assert handler.calls == 1
    assert request.status == ConfirmationStatus.APPROVED
    assert request.ui_ref is not None and request.ui_ref.message_id == MESSAGE_ID
    assert _ack_text(adapter) == "✅ Saved."
    chat_id, message_id, text = adapter.edit_message_text.await_args.args  # type: ignore[attr-defined]
    assert (chat_id, message_id) == (CHAT_ID, MESSAGE_ID)
    assert "User prefers tea." in text and "✅ Saved." in text


async def test_reject_answers_rejected_and_repeated_press_is_idempotent() -> None:
    adapter, handler = _adapter(), _Handler()
    service = _service(handler)
    request = await _pending(service)

    await _press(adapter, service, _button_data(adapter, request, 1))
    await _press(adapter, service, _button_data(adapter, request, 0))

    assert handler.calls == 0
    assert request.status == ConfirmationStatus.REJECTED
    assert _ack_text(adapter) == "❌ Rejected."


async def test_stale_callback_after_restart_is_answered_expired_not_sent_to_llm(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    request = await _pending(_service())
    data = _button_data(adapter, request, 0)
    store = FilesystemSessionStore(tmp_path)
    restarted = ConfirmationService(
        ConfirmationHandlerRegistry([_Handler()]), recorder=SessionConfirmationRecorder(store)
    )

    await _press(adapter, restarted, data)
    await _press(adapter, restarted, data)

    assert _ack_text(adapter) == NO_LONGER_VALID_TEXT
    _, message_id, text = adapter.edit_message_text.await_args.args  # type: ignore[attr-defined]
    assert message_id == MESSAGE_ID
    assert text.startswith("🧠 Save this to memory?") and NO_LONGER_VALID_TEXT in text
    # The agent learns the outcome: one `expired` record, even after repeated presses.
    contents = [
        str(r.payload.get("content", ""))
        for r in await store.replay_for_turn(SESSION, 50)
        if r.record_type == SessionRecordType.USER_MESSAGE
    ]
    assert len(contents) == 1
    payload = json.loads(contents[0].removeprefix(CONFIRMATION_RESOLVED_MARKER))
    assert (payload["confirmation_id"], payload["status"]) == (request.confirmation_id, "expired")


async def test_unknown_id_from_another_session_is_not_recorded() -> None:
    """The signature binds the session: a press is only recorded for the session that issued it."""
    adapter = _adapter()
    request = await _pending(_service())
    data = _button_data(adapter, request, 0)
    recorder = AsyncMock()
    restarted = ConfirmationService(ConfirmationHandlerRegistry([_Handler()]), recorder=recorder)
    orchestrator = _orchestrator()
    handler = _build_orchestrator_handler(adapter, orchestrator, confirmations=restarted)
    # The chat switched to a new session (/new) before the press.
    event = _callback_event(data).model_copy(update={"session_id": f"{SESSION}:new"})

    assert await handler(event) is None

    recorder.record_resolution.assert_not_awaited()
    assert _ack_text(adapter) == NO_LONGER_VALID_TEXT


async def test_known_request_resolves_after_chat_switched_session() -> None:
    adapter, handler = _adapter(), _Handler()
    service = _service(handler)
    request = await _pending(service)
    data = _button_data(adapter, request, 0)
    orchestrator = _orchestrator()
    callback = _build_orchestrator_handler(adapter, orchestrator, confirmations=service)
    event = _callback_event(data).model_copy(update={"session_id": f"{SESSION}:new"})

    assert await callback(event) is None

    assert handler.calls == 1 and request.status == ConfirmationStatus.APPROVED


@pytest.mark.parametrize(
    "data",
    ["mc:0123456789:66f1a2b3:abcdefabcdef", "cf:abc:a:0000000000000000", "cf:garbage"],
)
async def test_legacy_or_forged_callbacks_are_answered_explicitly(data: str) -> None:
    adapter, handler = _adapter(), _Handler()
    service = _service(handler)
    await _pending(service)

    await _press(adapter, service, data)

    assert handler.calls == 0
    assert _ack_text(adapter) == NO_LONGER_VALID_TEXT
    adapter.edit_message_text.assert_awaited_once()  # type: ignore[attr-defined]


async def test_ack_failure_is_logged_and_resolution_still_completes() -> None:
    adapter, handler = _adapter(), _Handler()
    adapter.acknowledge_callback = AsyncMock(side_effect=RuntimeError("telegram down"))  # type: ignore[method-assign]
    service = _service(handler)
    request = await _pending(service)

    await _press(adapter, service, _button_data(adapter, request, 0))

    assert request.status == ConfirmationStatus.APPROVED
    adapter.edit_message_text.assert_awaited_once()  # type: ignore[attr-defined]


async def test_normal_turn_sends_text_then_one_message_per_confirmation() -> None:
    adapter, service = _adapter(), _service()
    request = await _pending(service)
    orchestrator = _orchestrator(OrchestratorResult(text="Proposed.", confirmations=[request]))
    handler = _build_orchestrator_handler(adapter, orchestrator, confirmations=service)

    assert await handler(_text_event()) is None

    sent = [c.args[0] for c in adapter.send_response.await_args_list]  # type: ignore[attr-defined]
    assert [r.text for r in sent][0] == "Proposed."
    assert sent[1].ui_kind == "inline_keyboard" and "User prefers tea." in sent[1].text


async def test_ask_question_turn_also_renders_confirmation() -> None:
    adapter, service = _adapter(), _service()
    request = await _pending(service)
    ask = PendingAskData(
        question_id="q",
        question="Tea or coffee?",
        options=[{"id": "0", "label": "Tea"}],
        session_id=SESSION,
        turn_id="t",
        tool_call_id="q",
    )
    result = OrchestratorResult(text="", pending_ask=ask, confirmations=[request])
    handler = _build_orchestrator_handler(adapter, _orchestrator(result), confirmations=service)

    assert await handler(_text_event()) is None

    kinds = [c.args[0].ui_kind for c in adapter.send_response.await_args_list]  # type: ignore[attr-defined]
    assert kinds == ["reply_keyboard", "inline_keyboard"]


async def test_turn_without_confirmations_returns_response_unchanged() -> None:
    adapter, service = _adapter(), _service()
    handler = _build_orchestrator_handler(
        adapter, _orchestrator(OrchestratorResult(text="Hi")), confirmations=service
    )

    response = await handler(_text_event())

    assert response is not None and response.text == "Hi"
    adapter.send_response.assert_not_awaited()  # type: ignore[attr-defined]


async def test_undeliverable_confirmation_is_discarded() -> None:
    adapter, service = _adapter(), _service()
    adapter.send_response = AsyncMock(side_effect=RuntimeError("blocked"))  # type: ignore[method-assign]
    request = await _pending(service)

    await deliver_with_confirmations(
        adapter,
        service,
        response=None,
        confirmations=[request],
        chat_id=CHAT_ID,
        trace_id="t",
    )

    assert request.status == ConfirmationStatus.FAILED
    assert service.pending_for_session(SESSION) is None


async def test_delegation_feedback_turn_renders_confirmation_even_without_text() -> None:
    adapter, service = _adapter(), _service()
    request = await _pending(service)
    orchestrator = _orchestrator(OrchestratorResult(text="", confirmations=[request]))
    handler = _build_delegation_feedback_handler(orchestrator, adapter, service)
    now = datetime.now(UTC)
    task = TaskRecord(
        task_id="task-1",
        parent_session_id=SESSION,
        task_type="delegation",
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
        result={"summary": "done"},
    )

    await handler(task)

    sent = [c.args[0] for c in adapter.send_response.await_args_list]  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert sent[0].message_type == MessageType.INTERACTIVE
    assert sent[0].actions[0].callback_data.startswith("cf:")


async def test_delegation_feedback_text_precedes_confirmation() -> None:
    adapter, service = _adapter(), _service()
    request = await _pending(service)
    orchestrator = _orchestrator(OrchestratorResult(text="Task done.", confirmations=[request]))
    handler = _build_delegation_feedback_handler(orchestrator, adapter, service)
    now = datetime.now(UTC)
    task = TaskRecord(
        task_id="task-2",
        parent_session_id=SESSION,
        task_type="delegation",
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )

    await handler(task)

    texts = [c.args[0].text for c in adapter.send_response.await_args_list]  # type: ignore[attr-defined]
    assert texts[0] == "Task done."
    assert "User prefers tea." in texts[1]


async def test_max_size_memory_proposal_is_one_message_and_status_fits_edit() -> None:
    from assistant.channels.telegram.formatter import format_markdown_for_telegram
    from assistant.core.confirmation import MEMORY_BODY_MAX_CHARS, build_memory_update_draft
    from assistant.memory.write.models import MemoryUpdateIntent

    adapter, handler = _adapter(), _Handler()
    service = _service(handler)
    body = "Ж" * MEMORY_BODY_MAX_CHARS
    intent = MemoryUpdateIntent.model_validate(
        {
            "intent_id": "i",
            "action": "upsert",
            "memory_type": "facts",
            "candidate": {"body_markdown": body},
            "reason": "why " * 2000,
        }
    )
    request = (await service.request(SESSION, build_memory_update_draft(intent))).request
    assert request is not None

    await deliver_with_confirmations(
        adapter, service, response=None, confirmations=[request], chat_id=CHAT_ID, trace_id="t"
    )

    sent = [c.args[0] for c in adapter.send_response.await_args_list]  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert len(format_markdown_for_telegram(sent[0].text)) == 1
    assert body in sent[0].text  # the user sees exactly what will be saved

    await _press(adapter, service, _button_data(adapter, request, 0))

    edited = adapter.edit_message_text.await_args.args[2]  # type: ignore[attr-defined]
    assert len(edited.encode("utf-16-le")) // 2 <= 4096
    assert edited.endswith("✅ Saved.")
