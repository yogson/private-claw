"""Tests for memory schemas, paths, and parser."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.memory.store.models import (
    MemoryArtifact,
    MemoryFrontmatter,
    MemoryType,
)
from assistant.memory.store.parser import (
    parse_memory_content,
    parse_memory_file,
    serialize_memory_artifact,
)
from assistant.memory.store.paths import MemoryPaths


def test_memory_frontmatter_valid() -> None:
    fm = MemoryFrontmatter(
        memory_id="fact-001",
        type=MemoryType.FACTS,
        tags=["work", "deadline"],
        entities=["project-x"],
        priority=7,
        confidence=0.9,
        updated_at=datetime.now(UTC),
    )
    assert fm.memory_id == "fact-001"
    assert fm.type == MemoryType.FACTS
    assert fm.tags == ["work", "deadline"]


def test_memory_frontmatter_path_safe_rejects_slash() -> None:
    with pytest.raises(ValueError, match="path-safe"):
        MemoryFrontmatter(
            memory_id="bad/id",
            type=MemoryType.FACTS,
            updated_at=datetime.now(UTC),
        )


def test_memory_frontmatter_path_safe_accepts_valid_chars() -> None:
    fm = MemoryFrontmatter(
        memory_id="valid_id-123.abc",
        type=MemoryType.PROFILE,
        updated_at=datetime.now(UTC),
    )
    assert fm.memory_id == "valid_id-123.abc"


def test_memory_artifact_roundtrip() -> None:
    fm = MemoryFrontmatter(
        memory_id="pref-1",
        type=MemoryType.PREFERENCES,
        tags=["theme"],
        updated_at=datetime(2026, 3, 12, 10, 0, 0, tzinfo=UTC),
    )
    artifact = MemoryArtifact(frontmatter=fm, body="# Dark mode\nUser prefers dark theme.")
    serialized = serialize_memory_artifact(artifact)
    parsed = parse_memory_content(serialized)
    assert parsed.frontmatter.memory_id == artifact.frontmatter.memory_id
    assert parsed.frontmatter.type == artifact.frontmatter.type
    assert parsed.body == artifact.body


def test_parse_memory_content_valid() -> None:
    content = """---
memory_id: pref-1
type: preferences
tags: [theme, ui]
updated_at: 2026-03-12T10:00:00+00:00
---
# User preference
Dark mode enabled.
"""
    artifact = parse_memory_content(content)
    assert artifact.frontmatter.memory_id == "pref-1"
    assert artifact.frontmatter.type == MemoryType.PREFERENCES
    assert artifact.frontmatter.tags == ["theme", "ui"]
    assert "Dark mode" in artifact.body


def test_parse_memory_content_missing_frontmatter() -> None:
    with pytest.raises(ValueError, match="start with YAML frontmatter"):
        parse_memory_content("No frontmatter here")


def test_parse_memory_content_invalid_yaml() -> None:
    content = """---
memory_id: x
type: invalid: yaml: here
---
body
"""
    with pytest.raises(ValueError, match="Invalid YAML"):
        parse_memory_content(content)


def test_parse_memory_content_missing_required_field() -> None:
    content = """---
type: facts
tags: []
---
body
"""
    with pytest.raises(ValueError, match="memory_id"):
        parse_memory_content(content)


def test_memory_paths_layout(tmp_path: Path) -> None:
    paths = MemoryPaths(tmp_path)
    assert paths.memory_root == tmp_path / "memory"
    assert paths.indexes_dir == tmp_path / "runtime" / "memory_indexes"
    assert paths.category_dir(MemoryType.FACTS) == tmp_path / "memory" / "facts"
    assert (
        paths.artifact_path(MemoryType.PROFILE, "user-1")
        == tmp_path / "memory" / "profile" / "user-1.md"
    )
    assert (
        paths.index_path("index_by_type.json")
        == tmp_path / "runtime" / "memory_indexes" / "index_by_type.json"
    )


def test_parse_memory_file(tmp_path: Path) -> None:
    md_path = tmp_path / "test.md"
    md_path.write_text("""---
memory_id: file-test
type: summaries
updated_at: 2026-03-12T12:00:00+00:00
---
Summary body.
""")
    artifact = parse_memory_file(md_path)
    assert artifact.frontmatter.memory_id == "file-test"
    assert artifact.body.strip() == "Summary body."


def test_artifact_path_rejects_traversal(tmp_path: Path) -> None:
    paths = MemoryPaths(tmp_path)
    with pytest.raises(ValueError, match="path separators or"):
        paths.artifact_path(MemoryType.FACTS, "../outside")
    with pytest.raises(ValueError, match="path separators or"):
        paths.artifact_path(MemoryType.FACTS, "sub/escape")
    with pytest.raises(ValueError, match="path separators or"):
        paths.artifact_path(MemoryType.FACTS, "sub\\escape")


def test_artifact_path_rejects_invalid_chars(tmp_path: Path) -> None:
    paths = MemoryPaths(tmp_path)
    with pytest.raises(ValueError, match="path-safe"):
        paths.artifact_path(MemoryType.FACTS, "bad@id")


def test_parser_handles_literal_dashes_in_frontmatter() -> None:
    content = """---
memory_id: dash-test
type: facts
note: "value contains --- here"
updated_at: 2026-03-12T10:00:00+00:00
---
Body content.
"""
    artifact = parse_memory_content(content)
    assert artifact.frontmatter.memory_id == "dash-test"
    assert "Body content" in artifact.body


def test_parser_handles_literal_dashes_in_body() -> None:
    content = """---
memory_id: body-dash
type: summaries
updated_at: 2026-03-12T10:00:00+00:00
---
Body with --- separator
---
and more text
"""
    artifact = parse_memory_content(content)
    assert artifact.frontmatter.memory_id == "body-dash"
    assert "Body with --- separator" in artifact.body
    assert "---" in artifact.body
    assert "and more text" in artifact.body


def test_parser_preserves_body_whitespace_roundtrip() -> None:
    body_with_whitespace = "  leading spaces\n\n  trailing spaces  \n"
    content = (
        "---\n"
        "memory_id: ws-test\n"
        "type: preferences\n"
        "updated_at: 2026-03-12T10:00:00+00:00\n"
        "---\n"
        "\n" + body_with_whitespace
    )
    artifact = parse_memory_content(content)
    assert artifact.body == body_with_whitespace
    serialized = serialize_memory_artifact(artifact)
    parsed = parse_memory_content(serialized)
    assert parsed.body == body_with_whitespace
