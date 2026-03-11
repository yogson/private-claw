"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Factory for building VoiceTranscriptionService from TelegramChannelConfig.
Wires MTProto credentials and timeout into the service at startup.
"""

import structlog

from assistant.channels.telegram.ingestion.transcription import VoiceTranscriptionService
from assistant.core.config.schemas import TelegramChannelConfig

logger = structlog.get_logger(__name__)


def build_transcription_service(
    config: TelegramChannelConfig,
) -> VoiceTranscriptionService | None:
    """
    Build a VoiceTranscriptionService from config if MTProto credentials are present.

    Returns None when credentials are absent (transcription disabled).
    Logs a warning when credentials are present but no concrete worker
    implementation is available yet — this is the expected v1 baseline state
    until a Pyrogram/Telethon worker is introduced.
    """
    if config.mtproto_api_id is None or config.mtproto_api_hash is None:
        return None

    # Credentials are present. When a concrete MTProto worker is implemented,
    # construct it here and wrap it in VoiceTranscriptionService.
    # Until then, emit a structured warning so operators can identify the gap.
    logger.warning(
        "telegram.transcription.worker_not_implemented",
        mtproto_api_id=config.mtproto_api_id,
        transcription_timeout_seconds=config.transcription_timeout_seconds,
        note="MTProto credentials configured but no worker implementation available; "
        "transcription will not run until a concrete TranscriptionWorkerInterface is provided.",
    )
    return None
