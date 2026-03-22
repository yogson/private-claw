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


def test_list_context_ids_empty_when_no_sessions() -> None:
    service = ActiveSessionContextService()
    assert service.list_context_ids() == []


def test_list_context_ids_returns_all_known_contexts() -> None:
    service = ActiveSessionContextService()
    service.set_active_session("telegram:111", "tg:111:aaa")
    service.set_active_session("telegram:222", "tg:222:bbb")
    context_ids = service.list_context_ids()
    assert sorted(context_ids) == ["telegram:111", "telegram:222"]


def test_list_context_ids_excludes_cleared_sessions(tmp_path: Path) -> None:
    path = tmp_path / "ctx.json"
    service = ActiveSessionContextService(path)
    service.set_active_session("telegram:111", "tg:111:aaa")
    service.set_active_session("telegram:222", "tg:222:bbb")
    service.clear_active_session("telegram:111")
    assert service.list_context_ids() == ["telegram:222"]
