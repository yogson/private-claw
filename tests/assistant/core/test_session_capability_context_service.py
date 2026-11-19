"""
Unit tests for SessionCapabilityContextService.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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


# --- session-loaded capabilities (capability_load) ------------------------------------


def test_loaded_set_defaults_to_empty() -> None:
    assert SessionCapabilityContextService().get_loaded_capabilities("tg:1:a") == []


def test_loaded_set_roundtrips_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "capability_context.json"
    first = SessionCapabilityContextService(path)
    first.set_loaded_capabilities("tg:1:a", ["notes", "deploy", "notes"])
    first.set_capabilities("tg:1:a", ["assistant"])

    restarted = SessionCapabilityContextService(path)
    assert restarted.get_loaded_capabilities("tg:1:a") == ["notes", "deploy"]
    assert restarted.get_capabilities("tg:1:a") == ["assistant"]
    assert restarted.get_loaded_capabilities("tg:1:other") == []


def test_loaded_set_is_independent_of_the_override(tmp_path: Path) -> None:
    service = SessionCapabilityContextService(tmp_path / "ctx.json")
    service.set_loaded_capabilities("tg:1:a", ["notes"])
    service.set_capabilities("tg:1:a", ["assistant", "extra"])
    service.clear_capabilities("tg:1:a")
    assert service.get_loaded_capabilities("tg:1:a") == ["notes"]


def test_clearing_the_loaded_set_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "ctx.json"
    service = SessionCapabilityContextService(path)
    service.set_loaded_capabilities("tg:1:a", ["notes"])
    service.set_loaded_capabilities("tg:1:a", [])
    assert SessionCapabilityContextService(path).get_loaded_capabilities("tg:1:a") == []
    assert "tg:1:a" not in json.loads(path.read_text())["loaded"]


def test_returned_loaded_list_is_a_copy() -> None:
    service = SessionCapabilityContextService()
    service.set_loaded_capabilities("tg:1:a", ["notes"])
    service.get_loaded_capabilities("tg:1:a").append("mutated")
    assert service.get_loaded_capabilities("tg:1:a") == ["notes"]


def _write_loaded(path: Path, loaded_ts: str) -> None:
    path.write_text(
        json.dumps(
            {
                "__version": 2,
                "contexts": {},
                "timestamps": {},
                "loaded": {"tg:1:a": ["notes"]},
                "loaded_timestamps": {"tg:1:a": loaded_ts},
            }
        )
    )


def test_stale_loaded_set_is_pruned(tmp_path: Path) -> None:
    path = tmp_path / "ctx.json"
    _write_loaded(path, (datetime.now(UTC) - timedelta(days=31)).isoformat())
    assert SessionCapabilityContextService(path).get_loaded_capabilities("tg:1:a") == []


def test_touch_keeps_an_in_use_loaded_set_from_being_pruned(tmp_path: Path) -> None:
    path = tmp_path / "ctx.json"
    _write_loaded(path, (datetime.now(UTC) - timedelta(days=29)).isoformat())
    service = SessionCapabilityContextService(path)
    service.touch_loaded_capabilities("tg:1:a")  # a turn of the session
    with patch(
        "assistant.core.session_context.capability_context.datetime",
        wraps=datetime,
    ) as fake_dt:
        fake_dt.now.return_value = datetime.now(UTC) + timedelta(days=5)
        restarted = SessionCapabilityContextService(path)
    assert restarted.get_loaded_capabilities("tg:1:a") == ["notes"]


def test_touch_writes_at_most_once_a_day(tmp_path: Path) -> None:
    path = tmp_path / "ctx.json"
    service = SessionCapabilityContextService(path)
    service.set_loaded_capabilities("tg:1:a", ["notes"])
    before = path.read_text()
    service.touch_loaded_capabilities("tg:1:a")
    assert path.read_text() == before


def test_file_without_loaded_keys_still_loads(tmp_path: Path) -> None:
    """Files written before capability_load existed have no "loaded" map."""
    path = tmp_path / "ctx.json"
    now = datetime.now(UTC).isoformat()
    path.write_text(
        json.dumps({"__version": 2, "contexts": {"c": ["a"]}, "timestamps": {"c": now}})
    )
    service = SessionCapabilityContextService(path)
    assert service.get_capabilities("c") == ["a"]
    assert service.get_loaded_capabilities("c") == []


def test_malformed_loaded_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "ctx.json"
    now = datetime.now(UTC).isoformat()
    path.write_text(
        json.dumps(
            {
                "__version": 2,
                "contexts": {},
                "timestamps": {},
                "loaded": {"ok": ["notes", 3, " "], "bad": "notes", "": ["x"]},
                "loaded_timestamps": {"ok": now},
            }
        )
    )
    service = SessionCapabilityContextService(path)
    assert service.get_loaded_capabilities("ok") == ["notes"]
    assert service.get_loaded_capabilities("bad") == []
