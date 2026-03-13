"""Tests for prompt loader utilities."""

from pathlib import Path

import pytest

from assistant.core.prompts import load_prompt, resolve_prompts_dir


def test_resolve_prompts_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default resolves to src/prompts relative to cwd."""
    monkeypatch.delenv("ASSISTANT_PROMPTS_DIR", raising=False)
    result = resolve_prompts_dir()
    assert result == Path("src/prompts")


def test_resolve_prompts_dir_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """ASSISTANT_PROMPTS_DIR env var overrides default."""
    monkeypatch.setenv("ASSISTANT_PROMPTS_DIR", "/custom/prompts")
    result = resolve_prompts_dir()
    assert result == Path("/custom/prompts")


def test_resolve_prompts_dir_explicit_arg() -> None:
    """Explicit argument overrides env and default."""
    result = resolve_prompts_dir("/explicit/prompts")
    assert result == Path("/explicit/prompts")


def test_load_prompt_memory_agent_system() -> None:
    """Load memory_agent_system prompt from prompts store."""
    content = load_prompt("memory_agent_system")
    assert "helpful assistant" in content
    assert "memory_search" in content
    assert "memory_propose_update" in content
    assert "Do not write memory directly" in content


def test_load_prompt_rejects_path_traversal() -> None:
    """load_prompt rejects names with path separators or .."""
    with pytest.raises(ValueError, match="Invalid prompt name"):
        load_prompt("../etc/passwd")
    with pytest.raises(ValueError, match="Invalid prompt name"):
        load_prompt("subdir/../escape")


def test_load_prompt_not_found() -> None:
    """load_prompt raises FileNotFoundError for missing prompt."""
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_prompt_xyz")


def test_load_prompt_with_custom_dir(tmp_path: Path) -> None:
    """load_prompt accepts custom prompts_dir."""
    (tmp_path / "custom_prompt.md").write_text("Custom content", encoding="utf-8")
    content = load_prompt("custom_prompt", prompts_dir=tmp_path)
    assert content == "Custom content"
