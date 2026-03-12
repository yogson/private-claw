"""
Component ID: CMP_CORE_AGENT_ORCHESTRATOR

Protocol for downloading attachment bytes from channel storage (e.g. Telegram file_id).
Used by the orchestrator to fetch image/PDF content for multimodal LLM requests.
"""

from typing import Protocol


class AttachmentDownloaderInterface(Protocol):
    """Protocol for resolving attachment file_id to bytes."""

    async def download(
        self, file_id: str, mime_type: str, file_size_bytes: int, trace_id: str
    ) -> bytes | None:
        """
        Download attachment bytes from channel storage.

        Returns bytes on success, None on failure (network, size limit, etc.).
        """
        ...
