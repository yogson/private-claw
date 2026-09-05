"""Tests for append-only per-task activity logs."""

from pathlib import Path

import pytest

from assistant.store.filesystem.task_log import (
    _MAX_LOG_BYTES,
    append_log_line,
    read_log_tail,
    task_log_path,
)


def test_task_log_path_sanitizes_unsafe_characters(tmp_path: Path) -> None:
    path = task_log_path(tmp_path, "dlg-123/../../etc")
    assert path.parent == tmp_path
    assert ".." not in path.name
    assert "/" not in path.name


@pytest.mark.asyncio
async def test_append_and_read_tail_round_trip(tmp_path: Path) -> None:
    path = task_log_path(tmp_path, "dlg-1")
    await append_log_line(path, "[assistant] first")
    await append_log_line(path, "[tool_use] Bash(command=ls)")
    await append_log_line(path, "[tool_result] ok")

    assert read_log_tail(path, 10) == [
        "[assistant] first",
        "[tool_use] Bash(command=ls)",
        "[tool_result] ok",
    ]


@pytest.mark.asyncio
async def test_read_tail_respects_limit(tmp_path: Path) -> None:
    path = task_log_path(tmp_path, "dlg-1")
    for i in range(10):
        await append_log_line(path, f"line-{i}")

    assert read_log_tail(path, 3) == ["line-7", "line-8", "line-9"]


def test_read_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    path = task_log_path(tmp_path, "does-not-exist")
    assert read_log_tail(path, 10) == []


@pytest.mark.asyncio
async def test_append_strips_embedded_newlines_from_ragged_lines(tmp_path: Path) -> None:
    path = task_log_path(tmp_path, "dlg-1")
    await append_log_line(path, "line one\n")
    await append_log_line(path, "line two")

    assert read_log_tail(path, 10) == ["line one", "line two"]


@pytest.mark.asyncio
async def test_append_rotates_when_log_exceeds_cap(tmp_path: Path) -> None:
    path = task_log_path(tmp_path, "dlg-1")
    filler = "x" * 1000
    # Enough lines to clear _MAX_LOG_BYTES and trigger a rotation.
    line_count = (_MAX_LOG_BYTES // len(filler)) + 100
    for i in range(line_count):
        await append_log_line(path, f"{i}:{filler}")

    assert path.stat().st_size < _MAX_LOG_BYTES
    tail = read_log_tail(path, 5)
    # Rotation keeps the tail, so the most recent lines must still be present
    # and in order - the oldest content was the part dropped.
    assert tail[-1] == f"{line_count - 1}:{filler}"
    assert tail == sorted(tail, key=lambda line: int(line.split(":", 1)[0]))
