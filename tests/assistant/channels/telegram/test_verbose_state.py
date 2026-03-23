"""
Unit tests for VerboseStateService — in-memory toggle and filesystem persistence.
"""

from pathlib import Path

from assistant.channels.telegram.verbose_state import VerboseStateService


class TestVerboseStateInMemory:
    """Baseline behaviour with no storage path (pure in-memory)."""

    def test_default_off(self) -> None:
        svc = VerboseStateService()
        assert svc.is_enabled(123) is False

    def test_toggle_on(self) -> None:
        svc = VerboseStateService()
        result = svc.toggle(123)
        assert result is True
        assert svc.is_enabled(123) is True

    def test_toggle_off(self) -> None:
        svc = VerboseStateService()
        svc.toggle(123)
        result = svc.toggle(123)
        assert result is False
        assert svc.is_enabled(123) is False

    def test_independent_chats(self) -> None:
        svc = VerboseStateService()
        svc.toggle(1)
        assert svc.is_enabled(1) is True
        assert svc.is_enabled(2) is False


class TestVerboseStatePersistence:
    """Verbose state must survive a service restart when a storage path is provided."""

    def test_enabled_state_persisted_and_restored(self, tmp_path: Path) -> None:
        path = tmp_path / "verbose_state.json"
        svc = VerboseStateService(storage_path=path)
        svc.toggle(42)

        restarted = VerboseStateService(storage_path=path)
        assert restarted.is_enabled(42) is True

    def test_disabled_state_persisted_and_restored(self, tmp_path: Path) -> None:
        path = tmp_path / "verbose_state.json"
        svc = VerboseStateService(storage_path=path)
        svc.toggle(42)  # on
        svc.toggle(42)  # off again

        restarted = VerboseStateService(storage_path=path)
        assert restarted.is_enabled(42) is False

    def test_multiple_chats_persisted(self, tmp_path: Path) -> None:
        path = tmp_path / "verbose_state.json"
        svc = VerboseStateService(storage_path=path)
        svc.toggle(1)
        svc.toggle(2)
        # chat 3 stays off

        restarted = VerboseStateService(storage_path=path)
        assert restarted.is_enabled(1) is True
        assert restarted.is_enabled(2) is True
        assert restarted.is_enabled(3) is False

    def test_storage_file_created_on_first_toggle(self, tmp_path: Path) -> None:
        path = tmp_path / "verbose_state.json"
        assert not path.exists()
        svc = VerboseStateService(storage_path=path)
        svc.toggle(99)
        assert path.exists()

    def test_corrupt_storage_falls_back_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "verbose_state.json"
        path.write_text("not-valid-json!!!")
        svc = VerboseStateService(storage_path=path)
        assert svc.is_enabled(1) is False

    def test_non_list_storage_falls_back_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "verbose_state.json"
        path.write_text('{"enabled": [1, 2]}')  # dict, not list
        svc = VerboseStateService(storage_path=path)
        assert svc.is_enabled(1) is False

    def test_no_storage_path_no_side_effects(self) -> None:
        """Without a storage path, toggle works normally and nothing is written to disk."""
        svc = VerboseStateService()
        svc.toggle(7)
        assert svc.is_enabled(7) is True
        # A second instance without a path starts fresh
        svc2 = VerboseStateService()
        assert svc2.is_enabled(7) is False
