"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Model selection flow for Telegram: list allowed models and switch
active model via signed inline keyboard callback selection.
"""

import uuid

from assistant.channels.telegram.model_select_callbacks import (
    sign_model_callback,
    verify_model_callback,
)
from assistant.channels.telegram.models import ActionButton, ChannelResponse, MessageType

_BUTTON_LABEL_MAX = 64


class ModelSelectService:
    """
    Builds Telegram model selection flows.

    Lists models from config model_allowlist and produces ChannelResponse
    objects with signed inline keyboard callbacks.
    """

    def __init__(
        self,
        model_allowlist: list[str],
        hmac_secret: str,
    ) -> None:
        self._model_allowlist = model_allowlist
        self._secret = hmac_secret.encode()

    def build_model_menu(
        self,
        current_session_id: str,
        chat_id: int,
        trace_id: str,
        current_model_id: str | None = None,
    ) -> ChannelResponse:
        """
        Build an interactive ChannelResponse with inline buttons for model selection.

        Each button carries a chat-scoped, timestamped signed payload.
        """
        if not self._model_allowlist:
            return ChannelResponse(
                response_id=str(uuid.uuid4()),
                channel="telegram",
                session_id=current_session_id,
                trace_id=trace_id,
                message_type=MessageType.TEXT,
                text="No models available for selection.",
            )

        lines = ["*Select LLM model for this session:*\n"]
        actions: list[ActionButton] = []
        for i, model_id in enumerate(self._model_allowlist, 1):
            marker = " ✓" if model_id == current_model_id else ""
            lines.append(f"{i}. `{model_id}`{marker}")
            button_label = f"{i}. {model_id}{marker}"
            actions.append(
                ActionButton(
                    label=button_label[:_BUTTON_LABEL_MAX],
                    callback_id=f"model:{model_id}",
                    callback_data=self.sign_callback(model_id, chat_id),
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
            ui_kind="model_select",
            actions=actions,
        )

    def sign_callback(self, model_id: str, chat_id: int) -> str:
        """Generate a chat-scoped, timestamped signed callback payload."""
        return sign_model_callback(model_id=model_id, chat_id=chat_id, secret=self._secret)

    def verify_callback(self, callback_data: str, expected_chat_id: int) -> str | None:
        """Verify a signed model-select callback and return model_id if valid."""
        return verify_model_callback(
            callback_data=callback_data,
            expected_chat_id=expected_chat_id,
            secret=self._secret,
        )
