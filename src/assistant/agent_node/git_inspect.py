"""
Component ID: CMP_API_AGENT_NODE_INGRESS

Read-only git inspection of a repo job's checkout (did HEAD move during the job?).
"""

import asyncio
import contextlib

_GIT_TIMEOUT_SECONDS = 15.0


async def _git(path: str, *args: str) -> str | None:
    """Run a read-only git command in ``path``; None on any failure."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, TimeoutError):
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="replace").strip()


async def head_sha(path: str) -> str | None:
    return await _git(path, "rev-parse", "HEAD")


async def current_branch(path: str) -> str | None:
    branch = await _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return None if branch in (None, "", "HEAD") else branch


async def diff_stat(path: str, before: str, after: str) -> str | None:
    stat = await _git(path, "diff", "--stat", f"{before}..{after}")
    return stat or None
