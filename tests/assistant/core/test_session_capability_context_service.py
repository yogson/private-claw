"""
Unit tests for SessionCapabilityContextService.
"""

from pathlib import Path

from assistant.core.session_context import SessionCapabilityContextService


def test_returns_none_when_no_override_set() -> None:
    service = SessionCapabilityContextService()
    assert service.get_capabilities("telegram:123") is None


def test_set_and_get_capabilities() -> None:
    service = SessionCapabilityContextService()
    service.set_capabilities("telegram:123", ["cap_a", "cap_b"])
    assert service.get_capabilities("telegram:123") == ["cap_a", "cap_b"]


def test_empty_list_is_valid_override() -> None:
    """An empty capability list (all disabled) must be stored and returned as-is."""
    service = SessionCapabilityContextService()
    service.set_capabilities("telegram:123", [])
    result = service.get_capabilities("telegram:123")
    # Must return [] not None — empty list is a deliberate override
    assert result == []


def test_clear_removes_override() -> None:
    service = SessionCapabilityContextService()
    service.set_capabilities("telegram:123", ["cap_a"])
    service.clear_capabilities("telegram:123")
    assert service.get_capabilities("telegram:123") is None


def test_clear_is_idempotent() -> None:
    service = SessionCapabilityContextService()
    service.clear_capabilities("telegram:123")  # no prior set — must not raise
    assert service.get_capabilities("telegram:123") is None


def test_multiple_contexts_are_independent() -> None:
    service = SessionCapabilityContextService()
    service.set_capabilities("telegram:111", ["cap_x"])
    service.set_capabilities("telegram:222", ["cap_y", "cap_z"])
    assert service.get_capabilities("telegram:111") == ["cap_x"]
    assert service.get_capabilities("telegram:222") == ["cap_y", "cap_z"]
    assert service.get_capabilities("telegram:333") is None


def test_override_replaces_previous_value() -> None:
    service = SessionCapabilityContextService()
    service.set_capabilities("telegram:123", ["cap_a", "cap_b"])
    service.set_capabilities("telegram:123", ["cap_c"])
    assert service.get_capabilities("telegram:123") == ["cap_c"]


def test_roundtrip_persisted(tmp_path: Path) -> None:
    path = tmp_path / "capability_context.json"
    first = SessionCapabilityContextService(path)
    first.set_capabilities("telegram:123", ["cap_a", "cap_b"])

    restarted = SessionCapabilityContextService(path)
    assert restarted.get_capabilities("telegram:123") == ["cap_a", "cap_b"]


def test_empty_list_persisted_and_loaded(tmp_path: Path) -> None:
    """An explicit empty-list override must survive a restart."""
    path = tmp_path / "capability_context.json"
    first = SessionCapabilityContextService(path)
    first.set_capabilities("telegram:123", [])

    restarted = SessionCapabilityContextService(path)
    assert restarted.get_capabilities("telegram:123") == []


def test_clear_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "capability_context.json"
    first = SessionCapabilityContextService(path)
    first.set_capabilities("telegram:123", ["cap_a"])
    first.clear_capabilities("telegram:123")

    restarted = SessionCapabilityContextService(path)
    assert restarted.get_capabilities("telegram:123") is None


def test_ignores_invalid_json_file(tmp_path: Path) -> None:
    path = tmp_path / "capability_context.json"
    path.write_text("not valid json")
    service = SessionCapabilityContextService(path)
    assert service.get_capabilities("telegram:123") is None


def test_ignores_non_dict_json_payload(tmp_path: Path) -> None:
    path = tmp_path / "capability_context.json"
    path.write_text('["list", "not", "dict"]')
    service = SessionCapabilityContextService(path)
    assert service.get_capabilities("telegram:123") is None


def test_strips_whitespace_from_context_id() -> None:
    service = SessionCapabilityContextService()
    service.set_capabilities("  telegram:123  ", ["cap_a"])
    assert service.get_capabilities("telegram:123") == ["cap_a"]


def test_no_storage_path_means_memory_only() -> None:
    service = SessionCapabilityContextService(storage_path=None)
    service.set_capabilities("telegram:123", ["cap_a"])
    # A second instance without a path starts empty — only in-memory
    service2 = SessionCapabilityContextService(storage_path=None)
    assert service2.get_capabilities("telegram:123") is None


def test_multiple_contexts_persisted(tmp_path: Path) -> None:
    path = tmp_path / "capability_context.json"
    first = SessionCapabilityContextService(path)
    first.set_capabilities("telegram:111", ["cap_x"])
    first.set_capabilities("telegram:222", ["cap_y", "cap_z"])

    restarted = SessionCapabilityContextService(path)
    assert restarted.get_capabilities("telegram:111") == ["cap_x"]
    assert restarted.get_capabilities("telegram:222") == ["cap_y", "cap_z"]
    assert restarted.get_capabilities("telegram:333") is None
