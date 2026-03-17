"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Telegram completion notifier for delegated tasks.
"""

import asyncio

import structlog

from assistant.channels.telegram.adapter import TelegramAdapter
from assistant.channels.telegram.models import ChannelResponse
from assistant.store.models import TaskRecord

logger = structlog.get_logger(__name__)

_NOTIFY_MAX_ATTEMPTS = 3
_NOTIFY_BASE_DELAY_SECONDS = 1.0


class DelegationTelegramNotifier:
    """Sends proactive Telegram completion notifications for delegated tasks."""

    def __init__(self, adapter: TelegramAdapter) -> None:
        self._adapter = adapter

    async def notify_task_terminal(self, task: TaskRecord) -> None:
        chat_id = task.metadata.get("chat_id")
        if chat_id is None:
            return
        text = self._build_message(task)
        response = self._adapter.build_task_result_response(
            chat_id=int(chat_id),
            session_id=task.parent_session_id or "",
            trace_id=str(task.metadata.get("trace_id", task.task_id)),
            task_id=task.task_id,
            status=task.status.value,
            summary=self._extract_summary(task),
            fallback_text=text,
        )
        await self._send_with_retry(response=response, chat_id=int(chat_id), task_id=task.task_id)

    @staticmethod
    def _extract_summary(task: TaskRecord) -> str:
        if isinstance(task.result, dict):
            summary = task.result.get("summary")
            if isinstance(summary, str):
                return summary.strip()
        return ""

    @staticmethod
    def _build_message(task: TaskRecord) -> str:
        status = task.status.value
        summary = DelegationTelegramNotifier._extract_summary(task)
        if summary:
            return f"Delegated task `{task.task_id}` finished with status *{status}*.\n\n{summary}"
        if task.error:
            return (
                f"Delegated task `{task.task_id}` finished with status *{status}*."
                f"\n\nError: {task.error}"
            )
        return f"Delegated task `{task.task_id}` finished with status *{status}*."

    async def _send_with_retry(
        self,
        *,
        response: ChannelResponse,
        chat_id: int,
        task_id: str,
    ) -> None:
        delay = _NOTIFY_BASE_DELAY_SECONDS
        for attempt in range(1, _NOTIFY_MAX_ATTEMPTS + 1):
            try:
                await self._adapter.send_response(response, chat_id=chat_id)
                return
            except Exception as exc:
                logger.warning(
                    "subagent.notify.retry",
                    task_id=task_id,
                    chat_id=chat_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt >= _NOTIFY_MAX_ATTEMPTS:
                    logger.exception(
                        "subagent.notify.failed",
                        task_id=task_id,
                        chat_id=chat_id,
                    )
                    return
                await asyncio.sleep(delay)
                delay *= 2
