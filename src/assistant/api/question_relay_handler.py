from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from assistant.channels.telegram import TelegramAdapter
from assistant.subagents.coordinator import DelegationCoordinator

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RelayedQuestion:
    """The Telegram messages a relayed question was sent as (the relay callback's handle)."""

    chat_id: int
    message_ids: list[int]
    text: str


def _build_question_relay_handler(
    adapter: TelegramAdapter,
) -> "Callable[[str, str, str, list[str]], Awaitable[RelayedQuestion]]":
    """Build a question-relay callback that sends AskUserQuestion to Telegram.

    The callback sends the question as a Telegram message.  The coordinator
    manages the asyncio.Future lifecycle and waits for the answer via
    :meth:`DelegationCoordinator.submit_delegation_answer`.  The returned
    :class:`RelayedQuestion` comes back to the expired handler.
    """

    async def _relay(
        task_id: str, session_id: str, question: str, options: list[str]
    ) -> RelayedQuestion:
        chat_id = DelegationCoordinator._chat_id_from_session(session_id)
        if chat_id is None:
            logger.warning(
                "subagent.question_relay.no_chat_id",
                task_id=task_id,
                session_id=session_id,
            )
            raise ValueError(f"Cannot relay question: no chat_id for session {session_id!r}")

        text = f"[Delegation task] {question}"
        response = adapter.build_delegation_question_response(
            chat_id=chat_id,
            session_id=session_id,
            trace_id=task_id,
            question=text,
            options=options,
        )
        # Propagate send errors so the coordinator can abort the wait and
        # surface a visible error to the user rather than silently swallowing.
        message_ids = await adapter.send_response_message_ids(response, chat_id=chat_id)
        return RelayedQuestion(chat_id=chat_id, message_ids=message_ids, text=text)

    return _relay


def _build_question_expired_handler(
    adapter: TelegramAdapter,
) -> "Callable[[str, str, Any, str], Awaitable[None]]":
    """Build the callback that marks a relayed question as no longer awaited.

    Edits the question's (last) message to ``notice`` followed by the question and
    removes its inline keyboard, so the user sees the question cannot be answered.
    """

    async def _expired(task_id: str, session_id: str, handle: Any, notice: str) -> None:
        if not isinstance(handle, RelayedQuestion) or not handle.message_ids:
            logger.info(
                "subagent.question_relay.expired_message_unknown",
                task_id=task_id,
                session_id=session_id,
            )
            return
        # The keyboard sits on the last chunk; a question split over several
        # messages keeps its earlier parts, so only the notice replaces the last one.
        text = f"{notice}\n\n{handle.text}" if len(handle.message_ids) == 1 else notice
        await adapter.edit_message_text(handle.chat_id, handle.message_ids[-1], text)

    return _expired
