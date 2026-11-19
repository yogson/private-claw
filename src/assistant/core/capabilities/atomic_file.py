"""
Component ID: CMP_CORE_CAPABILITIES

Atomic file replacement shared by the capabilities domain (manifest edits and their
history, ``~/.claude/settings.json``).
"""

import contextlib
import os
import stat
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, new_file_mode: int | None = None) -> None:
    """Replace ``path`` with ``data`` atomically: same-dir temp file, fsync, ``os.replace``.

    Readers see either the old or the new content, never a torn file. The existing file's
    permission bits are kept; a new file gets ``new_file_mode`` (default: the temp file's
    ``0600``). ``path`` must be the real file: replacing a symlink path would turn the link
    into a regular file, so callers resolve symlinks first. The temp name starts with a dot
    (``.<name>.*.tmp``), so it is never mistaken for a capability manifest.
    """
    try:
        mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = new_file_mode
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    fsync_directory(path.parent)


def fsync_directory(directory: Path) -> None:
    """Persist a rename in ``directory`` (best effort).

    Without it a power loss can bring the old file back (never a torn one). Platforms or
    filesystems that cannot open or fsync a directory (e.g. Windows) are ignored.
    """
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
