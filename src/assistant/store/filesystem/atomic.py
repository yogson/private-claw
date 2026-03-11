"""
Component ID: CMP_STORE_STATE_FACADE

Atomic file write utilities for filesystem backend.

Implements the atomic write pattern:
1. Write to temporary file in same directory
2. Flush and fsync
3. Atomically rename over target path
"""

import os
import tempfile
from pathlib import Path


async def atomic_write(path: Path, content: bytes) -> None:
    """
    Atomically write content to a file.

    Uses write-to-temp + rename pattern to ensure atomicity.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        os.write(fd, content)
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


async def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically write text content to a file."""
    await atomic_write(path, content.encode(encoding))


async def file_append_fsync(path: Path, content: bytes) -> None:
    """
    Append content to a file with fsync.

    Uses true file append mode. Safe for append-only logs where partial
    trailing writes can be detected and ignored on read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)


async def file_append_lines(path: Path, lines: list[str], encoding: str = "utf-8") -> None:
    """Append multiple lines to a file with fsync (adds newlines)."""
    content = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    await file_append_fsync(path, content.encode(encoding))


def ensure_directory(path: Path) -> None:
    """Ensure a directory exists, creating parents as needed."""
    path.mkdir(parents=True, exist_ok=True)


def safe_read_text(path: Path, default: str = "", encoding: str = "utf-8") -> str:
    """Safely read text from a file, returning default if file doesn't exist."""
    if not path.exists():
        return default
    return path.read_text(encoding=encoding)


def safe_read_bytes(path: Path, default: bytes = b"") -> bytes:
    """Safely read bytes from a file, returning default if file doesn't exist."""
    if not path.exists():
        return default
    return path.read_bytes()
