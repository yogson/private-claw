"""
Component ID: CMP_API_FASTAPI_GATEWAY

Routes delegation-completion callbacks to the ingress that owns the task.

The coordinator has a single completion callback. Tasks created by the agent-node
HTTP ingress carry ``metadata["source"] == "agent_node_http"`` and go to the webhook
path; everything else keeps going to the Telegram delegation-feedback handler.
"""

from collections.abc import Awaitable, Callable

import structlog

from assistant.agent_node.models import AGENT_NODE_SOURCE
from assistant.store.models import TaskRecord

logger = structlog.get_logger(__name__)

CompletionHandler = Callable[[TaskRecord], Awaitable[None]]


def build_completion_dispatcher(
    *,
    default_handler: CompletionHandler | None,
    agent_node_handler: CompletionHandler | None,
) -> CompletionHandler:
    """Build the coordinator completion callback."""

    async def _dispatch(task: TaskRecord) -> None:
        if task.metadata.get("source") == AGENT_NODE_SOURCE:
            if agent_node_handler is None:
                logger.warning("completion.agent_node_handler_missing", task_id=task.task_id)
                return
            await agent_node_handler(task)
            return
        if default_handler is not None:
            await default_handler(task)

    return _dispatch
