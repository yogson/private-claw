from collections.abc import Awaitable, Callable

import structlog

from assistant.channels.telegram import TelegramAdapter
from assistant.subagents.coordinator import DelegationCoordinator

logger = structlog.get_logger(__name__)


def _build_question_relay_handler(
    adapter: TelegramAdapter,
) -> "Callable[[str, str, str, list[str]], Awaitable[None]]":
    """Build a question-relay callback that sends AskUserQuestion to Telegram.

    The callback sends the question as a Telegram message.  The coordinator
    manages the asyncio.Future lifecycle and waits for the answer via
    :meth:`DelegationCoordinator.submit_delegation_answer`.
    """

    async def _relay(task_id: str, session_id: str, question: str, options: list[str]) -> None:
        chat_id = DelegationCoordinator._chat_id_from_session(session_id)
        if chat_id is None:
            logger.warning(
                "subagent.question_relay.no_chat_id",
                task_id=task_id,
                session_id=session_id,
            )
            return

        response = adapter.build_delegation_question_response(
            chat_id=chat_id,
            session_id=session_id,
            trace_id=task_id,
            question=f"[Delegation task] {question}",
            options=options,
        )
        # Propagate send errors so the coordinator can abort the wait and
        # surface a visible error to the user rather than silently swallowing.
        await adapter.send_response(response, chat_id=chat_id)

    return _relay
