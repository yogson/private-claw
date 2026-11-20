"""Tests for logging bootstrap: configure_logging and lifecycle integration."""

import json
import logging
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
import structlog

from assistant.core.config.schemas import AppConfig, LogLevel
from assistant.observability.correlation import reset_trace_id, set_trace_id
from assistant.observability.logging import configure_logging


@pytest.fixture()
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runtime_mode="prod",
        data_root=str(tmp_path),
        timezone="UTC",
        log_level=LogLevel.INFO,
    )


def test_configure_logging_creates_log_file_under_data_root_logs(
    app_config: AppConfig,
) -> None:
    log_path = configure_logging(app_config)
    assert log_path.parent == Path(app_config.data_root) / "logs"
    assert log_path.name.startswith("assistant-")
    assert log_path.name.endswith(".log")
    assert log_path.exists()


def test_configure_logging_writes_logs_to_file(app_config: AppConfig) -> None:
    log_path = configure_logging(app_config)
    structlog.get_logger("test").info("hello")
    content = log_path.read_text()
    parsed = json.loads(content.strip())
    assert parsed["event"] == "hello"
    assert parsed["logger"] == "test"
    assert parsed["level"] == "info"


def test_configure_logging_idempotent_handler_count(app_config: AppConfig) -> None:
    """Repeated configure_logging does not accumulate file handlers (no FD leak)."""
    for _ in range(3):
        configure_logging(app_config)
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1, "repeated configure must not leak FileHandlers"


def test_configure_logging_injects_trace_id_when_set(app_config: AppConfig) -> None:
    configure_logging(app_config)
    token = set_trace_id("trace-abc123")
    try:
        structlog.get_logger("test").info("with trace")
    finally:
        reset_trace_id(token)
    log_path = Path(app_config.data_root) / "logs"
    log_files = list(log_path.glob("*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text()
    parsed = json.loads(content.strip())
    assert parsed.get("trace_id") == "trace-abc123"


def test_configure_logging_respects_explicit_trace_id(app_config: AppConfig) -> None:
    """Explicit trace_id in log call is not overridden by context."""
    configure_logging(app_config)
    token = set_trace_id("context-trace")
    try:
        structlog.get_logger("test").info("explicit", trace_id="explicit-trace")
    finally:
        reset_trace_id(token)
    log_path = Path(app_config.data_root) / "logs"
    log_files = list(log_path.glob("*.log"))
    content = log_files[0].read_text()
    parsed = json.loads(content.strip())
    assert parsed["trace_id"] == "explicit-trace"
    assert parsed["event"] == "explicit"


def test_configure_logging_adds_instance_name_when_set(app_config: AppConfig) -> None:
    log_path = configure_logging(app_config.model_copy(update={"instance_name": "research"}))
    structlog.get_logger("test").info("labelled")
    logging.getLogger("third.party").warning("foreign")
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [line.get("instance") for line in lines] == ["research", "research"]


def test_configure_logging_omits_instance_by_default(app_config: AppConfig) -> None:
    log_path = configure_logging(app_config)
    structlog.get_logger("test").info("plain")
    parsed = json.loads(log_path.read_text().strip())
    assert "instance" not in parsed


def test_configure_logging_keeps_tracebacks_without_structlog_warning(
    app_config: AppConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """No format_exc_info/pretty-exceptions UserWarning; file and console keep tracebacks."""
    log_path = configure_logging(app_config)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            raise ValueError("boom-structlog")
        except ValueError:
            structlog.get_logger("test").exception("structlog failed")
        try:
            raise ValueError("boom-stdlib")
        except ValueError:
            logging.getLogger("foreign").exception("stdlib failed")

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    by_event = {line["event"]: line for line in lines}
    assert "boom-structlog" in by_event["structlog failed"]["exception"]
    assert "boom-stdlib" in by_event["stdlib failed"]["exception"]
    console = capsys.readouterr().err
    assert "Traceback" in console or "boom-structlog" in console
    assert "boom-stdlib" in console


def test_asyncio_iscoroutinefunction_deprecation_is_suppressed() -> None:
    """Importing the logging bootstrap filters only the uvloop-originated warning."""
    code = (
        "import asyncio, warnings\n"
        "warnings.simplefilter('always')\n"
        "import assistant.observability.logging\n"
        "asyncio.iscoroutinefunction(lambda: None)\n"
        "warnings.warn('other deprecation', DeprecationWarning)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert "other deprecation" in result.stderr
    assert "iscoroutinefunction" not in result.stderr
