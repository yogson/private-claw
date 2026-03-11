"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

TranscriptionWorkerInterface: abstract contract for the Telegram MTProto
voice transcription worker.
"""

from abc import ABC, abstractmethod


class TranscriptionWorkerInterface(ABC):
    """Abstract interface for the Telegram MTProto transcription worker."""

    @abstractmethod
    async def transcribe(self, file_id: str, duration_seconds: int) -> str | None:
        """
        Request transcription for a Telegram voice message.

        Returns transcript text on success, or None if the message is
        unsupported by the transcription service.
        Raises exceptions on network, permission, or quota failures.
        """
