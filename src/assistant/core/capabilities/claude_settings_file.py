"""
Component ID: CMP_CORE_CAPABILITIES

Safe read and atomic write of Claude Code's ``settings.json``.

The file belongs to the owner and to Claude Code itself; this code only merges a few keys
into it. Reads refuse anything that is not a JSON object (so a hand-edited or half-written
file is never replaced by a stripped-down one), and writes go to a temp file in the same
directory followed by ``os.replace`` and a best-effort fsync of the directory, so readers
never see a torn file and the rename survives a power loss where the platform allows it.
"""

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

# Mode for a settings file created from scratch: it may carry MCP server env (secrets).
NEW_SETTINGS_FILE_MODE = 0o600


class SettingsFileError(Exception):
    """The existing settings file cannot be read or is not a JSON object."""


def read_settings_file(path: Path) -> dict[str, Any]:
    """Return the JSON object stored at ``path`` (``{}`` when the file does not exist).

    Raises :class:`SettingsFileError` when the file is unreadable, not valid JSON or not
    a JSON object; callers must then leave the file alone.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsFileError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsFileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsFileError(f"{path} is not a JSON object (got {type(data).__name__})")
    return data


def write_settings_file(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace the settings at ``path`` with ``data`` (2-space indented JSON).

    A symlinked ``path`` (dotfiles setups) is resolved first, so the link survives and its
    target is updated. The existing file mode is kept; a new file gets
    :data:`NEW_SETTINGS_FILE_MODE`.
    """
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = NEW_SETTINGS_FILE_MODE
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself (best effort).

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
