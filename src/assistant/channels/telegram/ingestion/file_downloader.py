"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Downloads files from Telegram by file_id for multimodal LLM ingestion.
Uses aiogram Bot API (get_file + download_file).
"""

import structlog
from aiogram import Bot

logger = structlog.get_logger(__name__)


class TelegramFileDownloader:
    def __init__(self, bot_token: str, max_size_bytes: int) -> None:
        self._bot = Bot(token=bot_token)
        self._max_size = max_size_bytes

    async def download(
        self, file_id: str, mime_type: str, file_size_bytes: int, trace_id: str
    ) -> bytes | None:
        if file_size_bytes > self._max_size:
            logger.warning(
                "telegram.file_downloader.size_exceeded",
                file_id=file_id,
                file_size=file_size_bytes,
                max_size=self._max_size,
                trace_id=trace_id,
            )
            return None

        try:
            tg_file = await self._bot.get_file(file_id)
            if not tg_file.file_path:
                logger.warning(
                    "telegram.file_downloader.no_path",
                    file_id=file_id,
                    trace_id=trace_id,
                )
                return None
            buffer = await self._bot.download_file(tg_file.file_path)
            if buffer is None:
                return None
            buffer.seek(0)
            data = buffer.read()
            logger.info(
                "telegram.file_downloader.success",
                file_id=file_id,
                mime_type=mime_type,
                size_bytes=len(data),
                trace_id=trace_id,
            )
            return data
        except Exception as exc:
            logger.warning(
                "telegram.file_downloader.error",
                file_id=file_id,
                error=str(exc),
                trace_id=trace_id,
            )
            return None

    async def close(self) -> None:
        await self._bot.session.close()
