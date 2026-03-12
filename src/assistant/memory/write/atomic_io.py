"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Atomic file write for memory artifacts.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically write text using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".md")
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(temp_path, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
