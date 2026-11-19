"""
Component ID: CMP_CORE_CONFIRMATION_SERVICE

Capability-edit confirmation kind (``capability_edit``): draft building and the handler
that applies an approved manifest edit or rollback through CapabilityEditService.

The domain logic (validation, backup, atomic write, catalog reload) lives in
``assistant.core.capabilities.edit_service``; this module only adapts it to the generic
confirmation mechanism.
"""

from assistant.core.capabilities.edit_service import CapabilityEditService, EditProposal
from assistant.core.capabilities.edit_summary import inline
from assistant.core.confirmation.models import (
    ConfirmationAttachment,
    ConfirmationDraft,
    ConfirmationOutcome,
    ConfirmationRequest,
)

CAPABILITY_EDIT_KIND = "capability_edit"


def build_capability_edit_draft(
    proposal: EditProposal, origin_tool_call_id: str = "", origin_tool_name: str = ""
) -> ConfirmationDraft:
    """Wrap a validated proposal (edit or rollback) as a ``capability_edit`` draft.

    A prompt change travels as one attachment (the full diff, already split into parts),
    which the channel delivers before the buttons.
    """
    attachments = (
        [ConfirmationAttachment(title=proposal.prompt_title, parts=list(proposal.prompt_parts))]
        if proposal.prompt_parts
        else []
    )
    return ConfirmationDraft(
        kind=CAPABILITY_EDIT_KIND,
        payload=proposal.payload,
        summary_for_user=proposal.summary,
        dedupe_key=proposal.dedupe_key,
        origin_tool_call_id=origin_tool_call_id,
        origin_tool_name=origin_tool_name,
        attachments=attachments,
    )


class CapabilityEditHandler:
    """Applies approved capability edits; reports the real outcome (never a false 'applied')."""

    kind = CAPABILITY_EDIT_KIND

    def __init__(self, service: CapabilityEditService) -> None:
        self._service = service

    async def apply(
        self, request: ConfirmationRequest, *, user_id: str | None
    ) -> ConfirmationOutcome:
        """Re-check the precondition, back up, write atomically and reload the catalog."""
        outcome = await self._service.apply(
            request.payload,
            confirmation_id=request.confirmation_id,
            session_id=request.session_id,
            user_id=user_id,
        )
        # The detail is shown to the user (failed status line) and may quote manifest
        # content (e.g. validator messages naming tool ids): keep it to one safe line.
        return ConfirmationOutcome(
            ok=outcome.ok, status=outcome.status, detail=inline(outcome.detail)
        )
