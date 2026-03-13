"""
Component ID: CMP_OBSERVABILITY_LOGGING

Structured logging setup: configures structlog with stdlib integration and a file
handler. One log file per app run, stored under data_root/logs/.
"""

import logging
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from assistant.core.config.schemas import AppConfig, LogLevel
from assistant.observability.correlation import get_trace_id_from_context


def _log_level_to_int(level: LogLevel) -> int:
    return int(getattr(logging, level.value))


def _inject_trace_context(
    logger: object, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Inject trace_id from correlation context when set (e.g. inside request)."""
    trace_id = get_trace_id_from_context()
    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    return event_dict


def _log_file_path(data_root: str) -> Path:
    logs_dir = Path(data_root) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    pid = os.getpid()
    return logs_dir / f"assistant-{timestamp}-{pid}.log"


def configure_logging(app_config: AppConfig) -> Path:
    """Configure structlog and add a file handler. One file per app run.

    Returns the path to the log file.
    """
    log_path = _log_file_path(app_config.data_root)
    level = _log_level_to_int(app_config.log_level)

    # stdlib: configure root logger with file + console handlers.
    # structlog renders the message; we use %(message)s so the formatted output passes through.
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers when reloading (e.g. uvicorn --reload).
    # Close handlers to prevent FD leaks across repeated lifespan reloads.
    for h in root.handlers[:]:
        root.removeHandler(h)
        with suppress(Exception):
            h.close()

    # Processors for structlog-originated logs (logger is always set)
    structlog_processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _inject_trace_context,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]
    # Foreign pre-chain: same but WITHOUT filter_by_level. Third-party loggers
    # (anthropic, httpx, etc.) can pass None as logger, causing AttributeError.
    foreign_pre_chain = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _inject_trace_context,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    # structlog: build event dict, pass to ProcessorFormatter (no final renderer)
    structlog.configure(
        processors=structlog_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],  # type: ignore[arg-type]
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # File: JSON for machine parsing
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,  # type: ignore[arg-type]
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(file_formatter)
    root.addHandler(file_handler)

    # Console: human-readable
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,  # type: ignore[arg-type]
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
    )
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(console_formatter)
    root.addHandler(console)

    return log_path
