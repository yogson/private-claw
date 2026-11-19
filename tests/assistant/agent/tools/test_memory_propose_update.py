"""Tests for the memory_propose_update agent tool."""

from typing import Any
from unittest.mock import MagicMock

from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.memory_propose_update import memory_propose_update
from assistant.core.confirmation import (
    MEMORY_BODY_MAX_CHARS,
    ConfirmationDraft,
    ConfirmationHandlerRegistry,
    ConfirmationRequestResult,
    ConfirmationService,
)

SESSION = "tg:1"


def _ctx(service: ConfirmationService | None) -> MagicMock:
    ctx = MagicMock()
    ctx.tool_call_id = "tc-1"
    ctx.tool_name = "memory_propose_update"
    requester = None
    if service is not None:

        async def requester(draft: ConfirmationDraft) -> ConfirmationRequestResult:
            return await service.request(SESSION, draft)

    ctx.deps = TurnDeps(confirmation_requester=requester)
    return ctx


async def _propose(ctx: MagicMock, body: str = "User prefers tea.", **kw: Any) -> dict[str, Any]:
    return await memory_propose_update(
        ctx,
        intent_id=kw.get("intent_id", "i1"),
        action="upsert",
        memory_type="preferences",
        reason="explicit ask",
        source="explicit_user_request",
        requires_user_confirmation=False,
        candidate={"body_markdown": body},
    )


async def test_creates_pending_confirmation_with_note() -> None:
    service = ConfirmationService(ConfirmationHandlerRegistry())
    result = await _propose(_ctx(service))

    assert result["status"] == "pending_confirmation"
    assert result["deduplicated"] is False
    assert "do not re-propose" in result["note"].lower()
    pending = service.pending_for_session(SESSION)
    assert pending is not None and pending.confirmation_id == result["confirmation_id"]
    assert pending.origin_tool_call_id == "tc-1"
    assert "User prefers tea." in pending.summary_for_user


async def test_same_proposal_is_deduplicated_even_with_new_intent_id() -> None:
    service = ConfirmationService(ConfirmationHandlerRegistry())
    first = await _propose(_ctx(service))
    second = await _propose(_ctx(service), intent_id="other")

    assert second["status"] == "pending_confirmation"
    assert second["deduplicated"] is True
    assert second["confirmation_id"] == first["confirmation_id"]


async def test_different_proposal_while_one_is_pending_is_not_registered() -> None:
    service = ConfirmationService(ConfirmationHandlerRegistry())
    first = await _propose(_ctx(service))
    second = await _propose(_ctx(service), body="User lives in Athens.")

    assert second["status"] == "another_confirmation_pending"
    assert second["pending_confirmation_id"] == first["confirmation_id"]
    assert "retry" in second["note"].lower()


async def test_invalid_proposal_is_rejected() -> None:
    ctx = _ctx(ConfirmationService(ConfirmationHandlerRegistry()))
    result = await memory_propose_update(
        ctx,
        intent_id="",
        action="upsert",
        memory_type="facts",
        reason="",
        source="agent_inferred",
        requires_user_confirmation=True,
    )
    assert result["status"] == "rejected_invalid"


async def test_unavailable_without_confirmation_service() -> None:
    result = await _propose(_ctx(None))
    assert result["status"] == "unavailable"


async def test_body_longer_than_display_limit_is_rejected_not_truncated() -> None:
    service = ConfirmationService(ConfirmationHandlerRegistry())

    result = await _propose(_ctx(service), body="x" * (MEMORY_BODY_MAX_CHARS + 1))

    assert result["status"] == "rejected_invalid"
    assert str(MEMORY_BODY_MAX_CHARS) in result["reason"]
    assert service.pending_for_session(SESSION) is None


async def test_body_at_display_limit_is_shown_in_full() -> None:
    service = ConfirmationService(ConfirmationHandlerRegistry())
    body = "y" * MEMORY_BODY_MAX_CHARS

    result = await _propose(_ctx(service), body=body)

    assert result["status"] == "pending_confirmation"
    pending = service.pending_for_session(SESSION)
    assert pending is not None and body in pending.summary_for_user
    assert "truncated" not in pending.summary_for_user


async def test_draft_carries_the_runtime_tool_name() -> None:
    # The orchestrator's requester only accepts drafts from CONFIRMATION_TOOL_NAMES.
    drafts: list[ConfirmationDraft] = []
    service = ConfirmationService(ConfirmationHandlerRegistry())

    async def requester(draft: ConfirmationDraft) -> ConfirmationRequestResult:
        drafts.append(draft)
        return await service.request(SESSION, draft)

    ctx = MagicMock()
    ctx.tool_call_id = "tc-1"
    ctx.tool_name = "memory_propose_update"
    ctx.deps = TurnDeps(confirmation_requester=requester)

    result = await _propose(ctx)

    assert result["status"] == "pending_confirmation"
    assert [d.origin_tool_name for d in drafts] == ["memory_propose_update"]
