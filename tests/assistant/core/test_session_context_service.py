"""
Unit tests for ActiveSessionContextService.
"""

from pathlib import Path

from assistant.core.session_context import ActiveSessionContextService


def test_roundtrip_persisted_context(tmp_path: Path) -> None:
    path = tmp_path / "active_session_context.json"
    first = ActiveSessionContextService(path)
    first.set_active_session("telegram:123", "tg:123:abc")

    restarted = ActiveSessionContextService(path)
    assert restarted.get_active_session("telegram:123") == "tg:123:abc"


def test_clear_persisted_context(tmp_path: Path) -> None:
    path = tmp_path / "active_session_context.json"
    first = ActiveSessionContextService(path)
    first.set_active_session("telegram:123", "tg:123:abc")
    first.clear_active_session("telegram:123")

    restarted = ActiveSessionContextService(path)
    assert restarted.get_active_session("telegram:123") is None


def test_ignores_invalid_json_payload(tmp_path: Path) -> None:
    path = tmp_path / "active_session_context.json"
    path.write_text("not json")
    service = ActiveSessionContextService(path)
    assert service.get_active_session("telegram:123") is None
