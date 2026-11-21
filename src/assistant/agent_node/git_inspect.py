"""
Component ID: CMP_API_AGENT_NODE_INGRESS

Small async git helpers: inspection of a repo job's checkout (did HEAD move during the
job?) and worktree management. Also used by the delegation coordinator's sub-agent
workspaces (``assistant.subagents.workspaces``).
"""

import asyncio
import contextlib
from dataclasses import dataclass

_GIT_TIMEOUT_SECONDS = 15.0
# Network operations (fetch) get a longer budget than local inspection.
GIT_FETCH_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class GitResult:
    """Outcome of one git invocation; ``returncode`` is -1 when git could not run."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def error(self) -> str:
        return self.stderr or self.stdout or f"git exited {self.returncode}"


async def run_git(path: str, *args: str, timeout: float = _GIT_TIMEOUT_SECONDS) -> GitResult:
    """Run ``git -C path *args``; never raises (spawn errors and timeouts give rc -1)."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, TimeoutError) as exc:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        return GitResult(-1, "", f"{type(exc).__name__}: {exc}")
    return GitResult(
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", errors="replace").strip(),
        err.decode("utf-8", errors="replace").strip(),
    )


async def git_output(path: str, *args: str) -> str | None:
    """Run a git command in ``path``; its stdout, or None on any failure."""
    result = await run_git(path, *args)
    return result.stdout if result.ok else None


_git = git_output


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


async def worktree_remove_clean(repo: str, worktree: str) -> bool:
    """Remove the worktree only if git agrees it is clean (no ``--force``)."""
    return await _git(repo, "worktree", "remove", worktree) is not None


async def diff_stat(path: str, before: str, after: str) -> str | None:
    stat = await _git(path, "diff", "--stat", f"{before}..{after}")
    return stat or None


async def branch_delete(repo: str, branch: str) -> bool:
    return await _git(repo, "branch", "-D", branch) is not None


async def current_branch(path: str) -> str | None:
    """Short name of the branch checked out in ``path``; None when detached or not git."""
    return await _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")


async def worktree_prune(repo: str) -> bool:
    return await _git(repo, "worktree", "prune") is not None


async def worktree_list(repo: str) -> list[dict[str, str]] | None:
    """``git worktree list --porcelain`` as dicts (keys: worktree, HEAD, branch, ...)."""
    out = await _git(repo, "worktree", "list", "--porcelain")
    if out is None:
        return None
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


async def has_uncommitted_changes(path: str) -> bool | None:
    """Modified, staged or untracked (not ignored) files in ``path``; None if unknown."""
    # Explicit flags so status.showUntrackedFiles / submodule config cannot hide changes.
    result = await run_git(
        path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"
    )
    if not result.ok:
        return None
    return bool(result.stdout)
