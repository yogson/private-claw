"""The coordinator's single completion callback routes by task source."""

from datetime import UTC, datetime
from typing import Any

from assistant.api.completion_dispatcher import build_completion_dispatcher
from assistant.store.models import TaskRecord, TaskStatus


def _task(task_id: str, metadata: dict[str, Any]) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id=task_id,
        parent_session_id="tg:1" if "source" not in metadata else f"agent-node:{task_id}",
        task_type="delegation",
        status=TaskStatus.COMPLETED,
        created_at=now,
        updated_at=now,
        metadata=metadata,
    )


async def test_agent_node_tasks_go_to_the_webhook_path_only() -> None:
    telegram: list[str] = []
    webhook: list[str] = []

    async def tg(task: TaskRecord) -> None:
        telegram.append(task.task_id)

    async def hook(task: TaskRecord) -> None:
        webhook.append(task.task_id)

    dispatch = build_completion_dispatcher(default_handler=tg, agent_node_handler=hook)
    await dispatch(_task("a", {"source": "agent_node_http"}))
    assert (telegram, webhook) == ([], ["a"])


async def test_everything_else_reaches_telegram_unchanged() -> None:
    received: list[TaskRecord] = []
    webhook: list[str] = []

    async def tg(task: TaskRecord) -> None:
        received.append(task)

    async def hook(task: TaskRecord) -> None:
        webhook.append(task.task_id)

    dispatch = build_completion_dispatcher(default_handler=tg, agent_node_handler=hook)
    plain = _task("b", {"kind": "delegation"})
    other = _task("c", {"source": "something_else"})
    await dispatch(plain)
    await dispatch(other)
    assert received == [plain, other]
    assert received[0] is plain
    assert webhook == []


async def test_agent_node_task_without_a_handler_is_dropped_not_sent_to_telegram() -> None:
    telegram: list[str] = []

    async def tg(task: TaskRecord) -> None:
        telegram.append(task.task_id)

    dispatch = build_completion_dispatcher(default_handler=tg, agent_node_handler=None)
    await dispatch(_task("d", {"source": "agent_node_http"}))
    assert telegram == []


async def test_missing_default_handler_is_harmless() -> None:
    dispatch = build_completion_dispatcher(default_handler=None, agent_node_handler=None)
    await dispatch(_task("e", {}))


def test_coordinator_session_helpers_tolerate_the_synthetic_session_id() -> None:
    """No Telegram assumption may crash on ``agent-node:<job_id>``."""
    from assistant.subagents.coordinator import DelegationCoordinator

    assert DelegationCoordinator._chat_id_from_session("agent-node:abc") is None  # noqa: SLF001
    assert DelegationCoordinator._pending_question_key("agent-node:abc") == "agent-node:abc"  # noqa: SLF001
