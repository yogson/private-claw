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


async def resolve_ref(path: str, ref: str) -> str | None:
    """Commit sha ``ref`` points to, or None when it does not resolve."""
    return await _git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


async def resolve_base(path: str, base_branch: str) -> str | None:
    """Commit sha of ``base_branch``: the local branch, else ``origin/<base_branch>``."""
    return await resolve_ref(path, f"refs/heads/{base_branch}") or await resolve_ref(
        path, f"refs/remotes/origin/{base_branch}"
    )


async def branch_tip(path: str, branch: str) -> str | None:
    return await resolve_ref(path, f"refs/heads/{branch}")


async def worktree_add(repo: str, worktree: str, branch: str, start_sha: str) -> str | None:
    """Create ``worktree`` on a new ``branch`` at ``start_sha``. None on success, else the error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            repo,
            "worktree",
            "add",
            "-b",
            branch,
            worktree,
            start_sha,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, TimeoutError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return err.decode("utf-8", errors="replace").strip() or f"git exited {proc.returncode}"
    return None


async def worktree_remove(repo: str, worktree: str) -> bool:
    """Remove the worktree directory; the branch (and its commits) is kept."""
    return await _git(repo, "worktree", "remove", "--force", worktree) is not None


async def diff_stat(path: str, before: str, after: str) -> str | None:
    stat = await _git(path, "diff", "--stat", f"{before}..{after}")
    return stat or None


async def branch_delete(repo: str, branch: str) -> bool:
    return await _git(repo, "branch", "-D", branch) is not None
