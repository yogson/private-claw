"""
Component ID: CMP_STORE_TASK_PERSISTENCE

Append-only per-task log files for delegated sub-agent runs.

Kept separate from FilesystemTaskStore's TaskRecord JSON files: those are
rewritten whole-file on every status update (see atomic_write_text), which
does not scale to a live, growing activity log. A plain append-only file is
cheap to write from a hot message loop and cheap to tail-read.
"""

import re
from collections import deque
from pathlib import Path

from assistant.store.filesystem.atomic import atomic_write, file_append_fsync

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
# Rotation cap: once a task's log exceeds this many bytes, the oldest half is
# dropped so tail reads keep returning recent activity instead of stalling on
# content written before the cap was hit.
_MAX_LOG_BYTES = 2_000_000


def task_log_path(log_dir: Path, task_id: str) -> Path:
    safe_id = _SAFE_ID_RE.sub("_", task_id)
    return log_dir / f"{safe_id}.log"


async def append_log_line(path: Path, line: str) -> None:
    """Append one line to a task's log, rotating (keeping the tail) if needed.

    Best-effort: logging must never break the sub-agent run it's observing,
    so any OSError here is swallowed. Reuses this package's existing durable
    file primitives (fsync'd append, atomic rewrite-via-rename for rotation)
    instead of a separate, weaker hand-rolled append - every other write in
    this store is crash-safe and there's no reason for this one to differ.
    """
    try:
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            existing = path.read_bytes()[-(_MAX_LOG_BYTES // 2) :]
            # The cut point likely lands mid-line; drop that partial line.
            newline_at = existing.find(b"\n")
            if newline_at != -1:
                existing = existing[newline_at + 1 :]
            await atomic_write(path, existing)
        await file_append_fsync(path, (line.rstrip("\n") + "\n").encode("utf-8"))
    except OSError:
        pass


def read_log_tail(path: Path, tail_lines: int) -> list[str]:
    """Return up to the last ``tail_lines`` lines of a task's log (empty if none)."""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=tail_lines)]
    except OSError:
        return []
