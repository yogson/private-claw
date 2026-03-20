"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Capability selection flow for Telegram: list available capabilities and toggle
active capabilities via signed inline keyboard callbacks.
"""

import uuid

from assistant.channels.telegram.capability_select_callbacks import (
    sign_capability_callback,
    verify_capability_callback,
)
from assistant.channels.telegram.models import ActionButton, ChannelResponse, MessageType
from assistant.core.capabilities.schemas import CapabilityDefinition

_BUTTON_LABEL_MAX = 64


class CapabilitySelectService:
    """
    Builds Telegram capability selection flows.

    Lists capability definitions and produces ChannelResponse objects with
    signed inline keyboard callbacks for toggling capabilities on/off.
    """

    def __init__(
        self,
        capability_definitions: dict[str, CapabilityDefinition],
        hmac_secret: str,
    ) -> None:
        self._definitions = capability_definitions
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
        Enabled capabilities are marked with a checkmark (✓).
        """
        if not self._definitions:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=current_session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text="No capabilities available.",
            )

        enabled_set = set(enabled_capabilities)
        lines = ["*Select capabilities for this session:*\n"]
        actions: list[ActionButton] = []
        for i, cap_id in enumerate(sorted(self._definitions), 1):
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
