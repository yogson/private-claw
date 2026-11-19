"""
Integration tests for the Telegram confirmation flow: rendering after turns and
Approve/Reject callback handling (CMP_API_FASTAPI_GATEWAY + CMP_CHANNEL_TELEGRAM_ADAPTER).
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.api.confirmation_handler import deliver_with_confirmations
from assistant.api.delegation_feedback_handler import _build_delegation_feedback_handler
from assistant.api.orchestrator_handler import _build_orchestrator_handler
from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.confirmation_render import (
    NO_LONGER_VALID_TEXT,
    NOT_DELIVERED_TEXT,
)
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
    ConfirmationAttachment,
    ConfirmationDecision,
    ConfirmationDraft,
    ConfirmationHandlerRegistry,
    ConfirmationOutcome,
    ConfirmationRequest,
    ConfirmationService,
    ConfirmationStatus,
    SessionConfirmationRecorder,
    unresolved_pending_ids,
)
from assistant.core.orchestrator.models import OrchestratorResult, PendingAskData
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.models import SessionRecordType, TaskRecord, TaskStatus
from tests.assistant.confirmation_helpers import persist_pending_proposal

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


def _recorded_service(tmp_path: Path) -> tuple[ConfirmationService, FilesystemSessionStore]:
    store = FilesystemSessionStore(tmp_path)
    service = ConfirmationService(
        ConfirmationHandlerRegistry([_Handler()]), recorder=SessionConfirmationRecorder(store)
    )
    return service, store


async def _resolution_statuses(store: FilesystemSessionStore) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for record in await store.replay_for_turn(SESSION, 200):
        content = str(record.payload.get("content", ""))
        if content.startswith(CONFIRMATION_RESOLVED_MARKER):
            payload = json.loads(content.removeprefix(CONFIRMATION_RESOLVED_MARKER))
            statuses[payload["confirmation_id"]] = payload["status"]
    return statuses


def _ask() -> PendingAskData:
    return PendingAskData(
        question_id="q",
        question="Tea or coffee?",
        options=[{"id": "0", "label": "Tea"}],
        session_id=SESSION,
        turn_id="t",
        tool_call_id="q",
    )


async def test_response_build_failure_after_turn_frees_slot_and_records_outcome(
    tmp_path: Path,
) -> None:
    """N8: the turn ended, then building the response raised before any delivery."""
    adapter = _adapter()
    service, store = _recorded_service(tmp_path)
    request = await _pending(service)
    await persist_pending_proposal(store, SESSION, request.confirmation_id)
    adapter.build_ask_question_response = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("render bug")
    )
    result = OrchestratorResult(text="", pending_ask=_ask(), confirmations=[request])
    handler = _build_orchestrator_handler(adapter, _orchestrator(result), confirmations=service)

    with pytest.raises(RuntimeError, match="render bug"):
        await handler(_text_event())

    assert request.status == ConfirmationStatus.FAILED
    adapter.send_response.assert_not_awaited()  # type: ignore[attr-defined]
    # The session slot is free: a new, different proposal is accepted immediately.
    retry = ConfirmationDraft(
        kind="memory_update", payload={}, summary_for_user="s", dedupe_key="2"
    )
    assert (await service.request(SESSION, retry)).created
    # The next turn's replay shows a terminal outcome for the undelivered id.
    assert (await _resolution_statuses(store))[request.confirmation_id] == "failed"
    assert unresolved_pending_ids(await store.replay_for_turn(SESSION, 200)) == []


async def test_confirmation_message_build_failure_discards_it(tmp_path: Path) -> None:
    adapter = _adapter()
    service, store = _recorded_service(tmp_path)
    request = await _pending(service)
    adapter.build_confirmation_response = MagicMock(  # type: ignore[method-assign]
        side_effect=ValueError("bad prompt")
    )

    sent = await deliver_with_confirmations(
        adapter, service, response=None, confirmations=[request], chat_id=CHAT_ID, trace_id="t"
    )

    assert sent is None
    assert request.status == ConfirmationStatus.FAILED
    assert service.pending_for_session(SESSION) is None
    assert (await _resolution_statuses(store))[request.confirmation_id] == "failed"


async def test_cancelled_delivery_discards_confirmation_and_reraises(tmp_path: Path) -> None:
    """N8: a /stop (task cancellation) while sending must not leave the slot taken."""
    adapter = _adapter()
    service, store = _recorded_service(tmp_path)
    request = await _pending(service)
    adapter.send_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=[True, asyncio.CancelledError()]
    )
    response = adapter.build_ask_question_response(
        session_id=SESSION, trace_id="t", question="Tea?", options=[{"id": "0", "label": "Tea"}]
    )

    with pytest.raises(asyncio.CancelledError):
        await deliver_with_confirmations(
            adapter,
            service,
            response=response,
            confirmations=[request],
            chat_id=CHAT_ID,
            trace_id="t",
        )

    assert request.status == ConfirmationStatus.FAILED
    assert service.pending_for_session(SESSION) is None
    assert (await _resolution_statuses(store))[request.confirmation_id] == "failed"


class _GatedRecorder:
    """Blocks inside record_resolution until released, then writes the real record."""

    def __init__(self, store: FilesystemSessionStore) -> None:
        self._inner = SessionConfirmationRecorder(store)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.written = asyncio.Event()

    async def record_resolution(self, request: ConfirmationRequest) -> None:
        self.entered.set()
        await self.release.wait()
        await self._inner.record_resolution(request)
        self.written.set()


async def test_second_cancellation_during_discard_still_writes_the_record(
    tmp_path: Path,
) -> None:
    """The discard is shielded: a cancel arriving while it runs does not abort it."""
    adapter = _adapter()
    store = FilesystemSessionStore(tmp_path)
    recorder = _GatedRecorder(store)
    service = ConfirmationService(ConfirmationHandlerRegistry([_Handler()]), recorder=recorder)
    request = await _pending(service)
    adapter.send_response = AsyncMock(return_value=False)  # type: ignore[method-assign]

    task = asyncio.create_task(
        deliver_with_confirmations(
            adapter, service, response=None, confirmations=[request], chat_id=CHAT_ID, trace_id="t"
        )
    )
    await asyncio.wait_for(recorder.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recorder.release.set()
    await asyncio.wait_for(recorder.written.wait(), timeout=2)

    assert request.status == ConfirmationStatus.FAILED
    assert (await _resolution_statuses(store))[request.confirmation_id] == "failed"


async def test_second_cancellation_during_discard_still_frees_the_slot() -> None:
    """A cancel while the discard waits for the per-id lock must not leave it pending."""
    adapter, service = _adapter(), _service()
    request = await _pending(service)
    adapter.send_response = AsyncMock(return_value=False)  # type: ignore[method-assign]

    async with service._lock_for(request.confirmation_id):  # e.g. a press being processed
        task = asyncio.create_task(
            deliver_with_confirmations(
                adapter,
                service,
                response=None,
                confirmations=[request],
                chat_id=CHAT_ID,
                trace_id="t",
            )
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task.done() and request.is_pending
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for _ in range(10):
        await asyncio.sleep(0)

    assert request.status == ConfirmationStatus.FAILED
    assert service.pending_for_session(SESSION) is None


async def test_press_on_undelivered_confirmation_says_no_longer_valid(tmp_path: Path) -> None:
    """The send can fail after the message arrived; the toast must not claim otherwise."""
    adapter = _adapter()
    service, store = _recorded_service(tmp_path)
    request = await _pending(service)
    data = _button_data(adapter, request, 0)
    adapter.send_response = AsyncMock(side_effect=TimeoutError("timed out"))  # type: ignore[method-assign]
    await deliver_with_confirmations(
        adapter, service, response=None, confirmations=[request], chat_id=CHAT_ID, trace_id="t"
    )
    assert request.status == ConfirmationStatus.FAILED

    await _press(adapter, service, data)

    assert _ack_text(adapter) == NOT_DELIVERED_TEXT
    assert "could not be delivered" not in _ack_text(adapter)
    edited = adapter.edit_message_text.await_args.args[2]  # type: ignore[attr-defined]
    assert edited.endswith(NOT_DELIVERED_TEXT)
    # The agent-facing record keeps the precise reason.
    stored = await store.read_session(SESSION)
    (record,) = [r for r in stored if r.event_id == f"confirmation-{request.confirmation_id}"]
    assert "could not be delivered" in record.payload["content"]


async def test_send_reporting_false_is_treated_as_not_delivered() -> None:
    adapter, service = _adapter(), _service()
    adapter.send_response = AsyncMock(return_value=False)  # type: ignore[method-assign]
    request = await _pending(service)

    await deliver_with_confirmations(
        adapter, service, response=None, confirmations=[request], chat_id=CHAT_ID, trace_id="t"
    )

    assert request.status == ConfirmationStatus.FAILED


async def test_delivered_confirmation_stays_pending() -> None:
    adapter, service = _adapter(), _service()
    request = await _pending(service)

    await deliver_with_confirmations(
        adapter, service, response=None, confirmations=[request], chat_id=CHAT_ID, trace_id="t"
    )

    assert request.is_pending
    assert service.pending_for_session(SESSION) is request


async def test_delegation_feedback_response_build_failure_frees_slot() -> None:
    adapter, service = _adapter(), _service()
    request = await _pending(service)
    adapter.build_ask_question_response = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("render bug")
    )
    result = OrchestratorResult(text="", pending_ask=_ask(), confirmations=[request])
    handler = _build_delegation_feedback_handler(_orchestrator(result), adapter, service)
    now = datetime.now(UTC)
    task = TaskRecord(
        task_id="task-x",
        parent_session_id=SESSION,
        task_type="delegation",
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(RuntimeError, match="render bug"):
        await handler(task)

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


# --- S9: attachment parts are delivered in full before the buttons ----------------


def _attachment_adapter(fail_part: int | None = None) -> tuple[TelegramAdapter, list[Any]]:
    """Adapter recording every send in order; part sends get ids 1001, 1002, ..."""
    adapter = _adapter()
    sends: list[Any] = []

    async def send_ids(response: Any, chat_id: int) -> list[int]:
        sends.append(response)
        if fail_part is not None and len(sends) == fail_part:
            raise RuntimeError("telegram down")
        return [1000 + len(sends)]

    async def send(response: Any, chat_id: int) -> bool:
        sends.append(response)
        return True

    adapter.send_response_message_ids = AsyncMock(side_effect=send_ids)  # type: ignore[method-assign]
    adapter.send_response = AsyncMock(side_effect=send)  # type: ignore[method-assign]
    return adapter, sends


async def _pending_with_parts(
    service: ConfirmationService, parts: list[str]
) -> ConfirmationRequest:
    draft = ConfirmationDraft(
        kind="capability_edit",
        payload={},
        summary_for_user="Capability edit: cap\n  • prompt: changed (see parts)",
        dedupe_key="k",
        attachments=[ConfirmationAttachment(title="Prompt change for cap", parts=parts)],
    )
    request = (await service.request(SESSION, draft)).request
    assert request is not None
    return request


async def _deliver(adapter: TelegramAdapter, service: ConfirmationService, request: Any) -> None:
    await deliver_with_confirmations(
        adapter,
        service,
        response=None,
        confirmations=[request],
        chat_id=CHAT_ID,
        trace_id="t",
    )


async def test_parts_are_sent_before_the_buttons_which_reply_to_part_1() -> None:
    adapter, sends = _attachment_adapter()
    service = _service()
    parts = ["+ " + "я" * 3698, "+ " + "🧠" * 1849, "- last line"]
    request = await _pending_with_parts(service, parts)
    short = request.confirmation_id[:8]

    await _deliver(adapter, service, request)

    assert len(sends) == 4
    *part_msgs, buttons = sends
    for index, (message, part) in enumerate(zip(part_msgs, parts, strict=True), start=1):
        assert message.text == (
            f"Prompt change for cap — part {index}/3 (confirmation {short})\n\n{part}"
        )
        assert message.plain_text is True and not message.actions
        assert len(message.text.encode("utf-16-le")) // 2 <= 4096
    assert part_msgs[0].reply_to_message_id is None
    assert [m.reply_to_message_id for m in part_msgs[1:]] == [1001, 1001]
    assert buttons.ui_kind == "inline_keyboard" and len(buttons.actions) == 2
    assert buttons.reply_to_message_id == 1001
    assert f"This message is a reply to part 1 (confirmation {short})" in buttons.text
    assert "Prompt change for cap (3 parts)" in buttons.text
    assert request.status == ConfirmationStatus.PENDING


@pytest.mark.parametrize("failing", [1, 2, 3])
async def test_failed_part_fails_the_confirmation_frees_the_slot_and_sends_no_buttons(
    failing: int,
) -> None:
    adapter, sends = _attachment_adapter(fail_part=failing)
    service = _service()
    request = await _pending_with_parts(service, ["p1", "p2", "p3"])

    await _deliver(adapter, service, request)

    assert len(sends) == failing  # nothing after the failed part
    assert not any(s.actions for s in sends)
    adapter.send_response.assert_not_awaited()  # type: ignore[attr-defined]
    assert request.status == ConfirmationStatus.FAILED
    assert request.result is not None and request.result.status == "not_delivered"
    assert f"Part {failing} of 3" in request.detail and "buttons were not shown" in request.detail
    assert service.pending_for_session(SESSION) is None
    assert (await _pending(service)).is_pending  # the session slot is free again


async def test_part_send_reporting_no_message_id_counts_as_not_delivered() -> None:
    adapter, sends = _attachment_adapter()
    adapter.send_response_message_ids = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service = _service()
    request = await _pending_with_parts(service, ["p1"])

    await _deliver(adapter, service, request)

    adapter.send_response.assert_not_awaited()  # type: ignore[attr-defined]
    assert request.status == ConfirmationStatus.FAILED
    assert service.pending_for_session(SESSION) is None


async def test_button_failure_after_all_parts_fails_the_confirmation() -> None:
    adapter, sends = _attachment_adapter()
    adapter.send_response = AsyncMock(side_effect=RuntimeError("blocked"))  # type: ignore[method-assign]
    service = _service()
    request = await _pending_with_parts(service, ["p1", "p2"])

    await _deliver(adapter, service, request)

    assert len(sends) == 2
    assert request.status == ConfirmationStatus.FAILED
    assert service.pending_for_session(SESSION) is None


async def test_resolved_message_keeps_the_parts_note_and_fits() -> None:
    from assistant.channels.telegram.confirmation_render import render_resolved_text

    service = _service()
    request = await _pending_with_parts(service, ["p1"])
    await service.resolve(request.confirmation_id, ConfirmationDecision.REJECT)
    text = render_resolved_text("", request)
    assert "📎 Sent in full above: Prompt change for cap (1 part)" in text
    assert text.endswith("❌ Rejected.")


async def test_memory_confirmation_is_unaffected_by_attachments() -> None:
    adapter, sends = _attachment_adapter()
    service = _service()
    request = await _pending(service)

    await _deliver(adapter, service, request)

    adapter.send_response_message_ids.assert_not_awaited()  # type: ignore[attr-defined]
    (buttons,) = sends
    assert buttons.reply_to_message_id is None
    assert "📎" not in buttons.text and "part 1" not in buttons.text
    assert request.status == ConfirmationStatus.PENDING


async def test_max_size_capability_summary_with_parts_note_is_not_truncated() -> None:
    from assistant.core.confirmation import ATTACHMENT_TITLE_MAX_UTF16
    from assistant.core.confirmation.service import SUMMARY_MAX_CHARS

    adapter, sends = _attachment_adapter()
    service = _service()
    draft = ConfirmationDraft(
        kind="capability_edit",
        payload={},
        summary_for_user="s" * SUMMARY_MAX_CHARS,
        dedupe_key="k",
        attachments=[
            ConfirmationAttachment(title="t" * ATTACHMENT_TITLE_MAX_UTF16, parts=["p" * 3700])
        ],
    )
    request = (await service.request(SESSION, draft)).request
    assert request is not None

    await _deliver(adapter, service, request)

    part, buttons = sends
    assert "truncated" not in part.text and part.text.endswith("p" * 3700)
    assert "truncated" not in buttons.text and "read every part before deciding." in buttons.text
