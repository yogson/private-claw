"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Capability selection flow for Telegram: list available capabilities and toggle
active capabilities via signed inline keyboard callbacks.

The list is read from the capability catalog on every render, so manifests added,
removed or broken on disk show up without a restart.
"""

import uuid

from assistant.channels.telegram.capability_select_callbacks import (
    sign_capability_callback,
    verify_capability_callback,
)
from assistant.channels.telegram.models import ActionButton, ChannelResponse, MessageType
from assistant.core.capabilities.interfaces import CapabilityCatalogInterface
from assistant.core.capabilities.snapshot import CapabilitySnapshot

_BUTTON_LABEL_MAX = 64
_MAX_ERRORS_SHOWN = 3
_ERROR_MAX_CHARS = 200


class CapabilitySelectService:
    """
    Builds Telegram capability selection flows.

    Lists the capability definitions of the current catalog snapshot and produces
    ChannelResponse objects with signed inline keyboard callbacks for toggling
    capabilities on/off.
    """

    def __init__(
        self,
        capability_catalog: CapabilityCatalogInterface,
        hmac_secret: str,
    ) -> None:
        self._catalog = capability_catalog
        self._secret = hmac_secret.encode()

    def build_capabilities_menu(
        self,
        current_session_id: str,
        chat_id: int,
        trace_id: str,
        enabled_capabilities: list[str],
    ) -> ChannelResponse:
        """
        Build an interactive ChannelResponse with inline buttons for capability selection.

        Each button carries a chat-scoped, timestamped signed payload.
        Enabled capabilities are marked with a checkmark (✓). When the latest manifest
        reload failed, a warning lists the errors and the revision still in use.
        """
        snapshot = self._catalog.refresh()
        definitions = snapshot.definitions
        if not definitions:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=current_session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text="No capabilities available.",
            )

        enabled_set = set(enabled_capabilities)
        lines = [*_error_lines(snapshot), "*Select capabilities for this session:*\n"]
        actions: list[ActionButton] = []
        for i, cap_id in enumerate(sorted(definitions), 1):
            marker = " ✓" if cap_id in enabled_set else ""
            lines.append(f"{i}. `{cap_id}`{marker}")
            button_label = f"{i}. {cap_id}{marker}"
            actions.append(
                ActionButton(
                    label=button_label[:_BUTTON_LABEL_MAX],
                    callback_id=f"capability:{cap_id}",
                    callback_data=self.sign_callback(cap_id, chat_id),
                )
            )

        return ChannelResponse(
            response_id=str(uuid.uuid4()),
            channel="telegram",
            session_id=current_session_id,
            trace_id=trace_id,
            message_type=MessageType.INTERACTIVE,
            text="\n".join(lines),
            parse_mode="Markdown",
            ui_kind="capability_select",
            actions=actions,
        )

    def sign_callback(self, capability_id: str, chat_id: int) -> str:
        """Generate a chat-scoped, timestamped signed callback payload."""
        return sign_capability_callback(
            capability_id=capability_id, chat_id=chat_id, secret=self._secret
        )

    def verify_callback(self, callback_data: str, expected_chat_id: int) -> str | None:
        """Verify a signed capability-select callback and return capability_id if valid."""
        return verify_capability_callback(
            callback_data=callback_data,
            expected_chat_id=expected_chat_id,
            secret=self._secret,
        )


def _error_lines(snapshot: CapabilitySnapshot) -> list[str]:
    errors = snapshot.blocking_errors
    if not errors:
        return []
    lines = [
        f"⚠️ Capability manifests have errors; still using revision {snapshot.revision}:",
    ]
    for issue in errors[:_MAX_ERRORS_SHOWN]:
        message = issue.message
        if len(message) > _ERROR_MAX_CHARS:
            message = message[: _ERROR_MAX_CHARS - 1] + "…"
        lines.append(f"• `{message.replace('`', "'")}`")
    if len(errors) > _MAX_ERRORS_SHOWN:
        lines.append(f"• …and {len(errors) - _MAX_ERRORS_SHOWN} more (see logs)")
    lines.append("")
    return lines
