"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Parse and serialize markdown+frontmatter memory artifacts.
"""

from pathlib import Path

import yaml

from assistant.memory.store.models import MemoryArtifact, MemoryFrontmatter


def parse_memory_file(path: Path) -> MemoryArtifact:
    """Parse a markdown file with YAML frontmatter into a MemoryArtifact.

    Raises:
        ValueError: If file lacks valid frontmatter or frontmatter fails validation.
    """
    content = path.read_text(encoding="utf-8")
    return parse_memory_content(content)


def parse_memory_content(content: str) -> MemoryArtifact:
    """Parse markdown content with YAML frontmatter into a MemoryArtifact.

    Uses line-based delimiter detection: first line must be "---", then finds
    the next line that is exactly "---". Body is preserved as-is after the
    closing delimiter.

    Raises:
        ValueError: If content lacks valid frontmatter or frontmatter fails validation.
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("Memory file must start with YAML frontmatter (---)")

    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise ValueError("Invalid frontmatter: missing closing ---")

    raw_frontmatter = "\n".join(lines[1:end_idx])
    body_lines = lines[end_idx + 1 :]
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines) if body_lines else ""

    try:
        data = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML frontmatter: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    frontmatter = MemoryFrontmatter.model_validate(data)
    return MemoryArtifact(frontmatter=frontmatter, body=body)


def serialize_memory_artifact(artifact: MemoryArtifact) -> str:
    """Serialize a MemoryArtifact to markdown with YAML frontmatter.

    Body is emitted as-is (no trailing newline added) for roundtrip fidelity.
    """
    data = artifact.frontmatter.model_dump(mode="json", exclude_none=True)
    frontmatter_yaml = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    return f"---\n{frontmatter_yaml}---\n\n{artifact.body}"
