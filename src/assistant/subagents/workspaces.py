"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Workspaces of delegated sub-agents: where a task runs and who may write there.

Modes (``workspace.mode`` of a delegation request; only for requests with a directory):

- ``worktree`` (default for a git directory): the task runs in a coordinator-managed git
  worktree of the directory's repository, one per branch, under
  ``<worktrees_root>/<repo-key>/<branch-slug>``. An existing worktree for the branch is
  reused (its ``.venv``, uncommitted work etc. survive between tasks); a missing one is
  created from a freshly fetched base. At most one task owns a worktree at a time: an
  exclusive file lock, taken at enqueue and held until the task ends, so the rule holds
  across instances too. A second task on the same branch is rejected, not queued.
- ``readonly``: the task runs in the directory as given, without ownership or lock. The
  mode is a label plus prompt rules (no edits), it is not enforced.
- ``in_place`` (default for a non-git directory): the task runs in the directory as
  given. For a git directory an exclusive file lock on its checkout (``git rev-parse
  --show-toplevel``) under ``<shared_data_root>/locks/subagent_in_place`` is held while
  the task runs, so in_place tasks on one checkout run one after another, also across
  instances. A non-git directory gets no lock.

Cleanup (``WorkspaceManager.reconcile``) runs at startup and right before a new worktree
is created, never from task status. It only looks at worktrees this manager created
(state file under ``<worktrees_root>/.state``); a worktree is removed only when its
branch's PR is merged/closed (via ``gh``; without ``gh`` only the TTL applies) or it has
no open PR and was unused for the TTL, and only when it has no owner, no uncommitted
changes and no unpushed commits. After a merged PR the local branch is deleted only if
its own tip has no unpushed commits.
"""

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

import structlog
from pydantic import BaseModel, Field

from assistant.agent_node import git_inspect
from assistant.core.config.schemas import RuntimeConfig, ToolDefaultParams
from assistant.subagents.contracts import DelegationWorkspaceInfo

logger = structlog.get_logger(__name__)

_DEFAULT_TTL_DAYS = 7
_DEFAULT_IN_PLACE_LOCK_TIMEOUT_SECONDS = 1800
_LOCK_POLL_SECONDS = 1.0
_GH_TIMEOUT_SECONDS = 20.0
_AUTO_BASE_CANDIDATES = ("origin/master", "origin/main", "master", "main")
_AUTO_BRANCH_PREFIX = "subagent/"
_STATE_DIR = ".state"
_MAX_SLUG_LENGTH = 80


class WorkspaceMode(StrEnum):
    WORKTREE = "worktree"
    READONLY = "readonly"
    IN_PLACE = "in_place"


class WorkspaceError(Exception):
    """A workspace could not be prepared; ``reason`` is shown to the caller."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


_UNSAFE_REF_PARTS = ("..", "@{", ":", "+", "~", "^", "?", "*", "[", "\\")


def _is_safe_ref(ref: str) -> bool:
    """A plain ref name that git can never read as an option or a refspec."""
    return (
        not ref.startswith("-")
        and not any(ch.isspace() or not ch.isprintable() for ch in ref)
        and not any(part in ref for part in _UNSAFE_REF_PARTS)
    )


class WorkspaceRequest(BaseModel):
    """The ``workspace`` part of a delegation request."""

    mode: WorkspaceMode | None = None
    branch: str | None = None
    base: str | None = None

    @classmethod
    def parse(cls, raw: object) -> "WorkspaceRequest":
        """Validate a request's ``workspace`` dict; blank strings count as unset."""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise WorkspaceError("workspace must be an object with mode/branch/base")
        values: dict[str, Any] = {}
        for key in ("mode", "branch", "base"):
            value = raw.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise WorkspaceError(f"workspace {key} must be a string")
            if value.strip():
                values[key] = value.strip()
        mode = values.get("mode")
        if mode is not None and mode not in {m.value for m in WorkspaceMode}:
            allowed = ", ".join(m.value for m in WorkspaceMode)
            raise WorkspaceError(f"unknown workspace_mode '{mode}' (allowed: {allowed})")
        base = values.get("base")
        if base is not None and not _is_safe_ref(base):
            raise WorkspaceError(f"invalid base '{base}': expected a branch like origin/main")
        request = cls(**values)
        if request.mode not in (None, WorkspaceMode.WORKTREE) and (request.branch or request.base):
            raise WorkspaceError("branch and base apply only to workspace_mode 'worktree'")
        return request

    @property
    def is_empty(self) -> bool:
        return self.mode is None and self.branch is None and self.base is None


class ResolvedWorkspace(BaseModel):
    """Where a task runs; stored as ``metadata["workspace"]`` of the task."""

    mode: WorkspaceMode
    directory: str
    """Working directory of the run (the backend's cwd)."""
    requested_directory: str
    is_git: bool = False
    repo_root: str | None = None
    checkout_root: str | None = None
    """Toplevel of the requested directory's checkout (the in_place lock key)."""
    branch: str | None = None
    base: str | None = None
    base_sha: str | None = None
    worktree: str | None = None
    worktree_created: bool = False
    notes: list[str] = Field(default_factory=list)

    def info(self) -> DelegationWorkspaceInfo:
        """The caller-facing view: what the orchestrator needs for follow-up rounds."""
        is_worktree = self.mode == WorkspaceMode.WORKTREE
        return DelegationWorkspaceInfo(
            mode=self.mode.value,
            directory=self.directory,
            branch=self.branch if is_worktree else None,
            worktree=self.worktree if is_worktree else None,
            base=self.base if is_worktree else None,
            worktree_created=self.worktree_created if is_worktree else None,
        )


@dataclass
class _Ownership:
    task_id: str
    handle: IO[str]


@dataclass
class WorktreeCleanupReport:
    worktree: str
    branch: str
    action: str
    """``removed``, ``kept`` or ``forgotten`` (state of a worktree that no longer exists)."""
    reason: str


def _now() -> datetime:
    return datetime.now(UTC)


def _short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def branch_slug(branch: str) -> str:
    """Directory name for ``branch``: ``feat/x`` -> ``feat-x-<hash>``; ``fix-1`` -> ``fix-1``.

    A name changed by sanitizing gets a short hash of the branch so two branches never
    share a slug.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.")[:_MAX_SLUG_LENGTH]
    if slug != branch or not slug:
        slug = f"{slug or 'branch'}-{_short_hash(branch, 6)}"
    return slug


def repo_key(repo_root: str) -> str:
    """Directory name for a repository: ``<basename>-<hash of its path>``."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(repo_root).name).strip("-.") or "repo"
    return f"{name}-{_short_hash(repo_root)}"


def auto_branch_name(task_id: str) -> str:
    """Branch for a worktree task that names none: ``subagent/<task_id>``."""
    return f"{_AUTO_BRANCH_PREFIX}{task_id}"


def _try_lock(path: Path) -> IO[str] | None:
    """Open ``path`` and take an exclusive non-blocking flock; None when held elsewhere.

    ``flock`` locks belong to the open file, so a second open in the same process
    conflicts too. The OS drops the lock when the process dies.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - kept open while held
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except OSError:
        handle.close()
        raise
    return handle


def _write_holder(handle: IO[str], info: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(info))
    handle.flush()


def _read_holder(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _unlock(handle: IO[str]) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _holder_text(info: dict[str, Any]) -> str:
    task_id = info.get("task_id") or "unknown task"
    instance = info.get("instance")
    return f"{task_id} (instance {instance})" if instance else str(task_id)


def _tool_defaults(config: RuntimeConfig) -> ToolDefaultParams:
    for tool in config.tools.tools:
        if tool.tool_id == "delegate_subagent_task" and tool.default_params is not None:
            return tool.default_params
    return ToolDefaultParams()


class WorkspaceManager:
    """Resolves, owns, locks and cleans up the workspaces of delegated tasks."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        gh_runner: Callable[[str, list[str]], Awaitable[tuple[int, str]]] | None = None,
        lock_poll_seconds: float = _LOCK_POLL_SECONDS,
    ) -> None:
        defaults = _tool_defaults(config)
        shared_root = Path(config.app.effective_shared_data_root).expanduser()
        root = (
            Path(defaults.delegation_worktrees_root).expanduser()
            if defaults.delegation_worktrees_root
            else shared_root / "subagent_worktrees"
        )
        self.root = root.resolve()
        self._state_dir = self.root / _STATE_DIR
        self._in_place_lock_dir = (shared_root / "locks" / "subagent_in_place").resolve()
        self._default_base = (defaults.delegation_worktree_default_base or "").strip() or None
        self.ttl = timedelta(days=defaults.delegation_worktree_ttl_days or _DEFAULT_TTL_DAYS)
        self._delete_merged_branches = defaults.delegation_worktree_delete_merged_branches
        if self._delete_merged_branches is None:
            self._delete_merged_branches = True
        self.in_place_lock_timeout = float(
            defaults.delegation_in_place_lock_timeout_seconds
            or _DEFAULT_IN_PLACE_LOCK_TIMEOUT_SECONDS
        )
        self._instance = config.app.instance_name or ""
        self._gh_runner = gh_runner or _run_gh
        self._lock_poll_seconds = lock_poll_seconds
        # Worktree path -> owning task (and its held lock), for tasks of this process.
        self._owners: dict[str, _Ownership] = {}
        # Serializes resolution and cleanup inside this process; the file locks cover
        # other instances.
        self._resolve_lock = asyncio.Lock()

    # ------------------------------------------------------------------ resolution

    async def resolve(
        self,
        *,
        task_id: str,
        directory: str,
        request: WorkspaceRequest,
        is_task_active: Callable[[str], Awaitable[bool]] | None = None,
    ) -> ResolvedWorkspace:
        """Resolve where ``task_id`` runs; claims the worktree in ``worktree`` mode.

        Raises WorkspaceError with a caller-facing reason. ``is_task_active`` lets a
        stale in-process owner (a task that ended without releasing) be detected.
        """
        toplevel = await git_inspect.git_output(directory, "rev-parse", "--show-toplevel")
        mode = request.mode
        if toplevel is None:
            if mode == WorkspaceMode.WORKTREE:
                raise WorkspaceError(
                    f"workspace_mode 'worktree' needs a git repository; {directory} is not "
                    "one. Use workspace_mode 'in_place' or 'readonly'."
                )
            if request.branch or request.base:
                raise WorkspaceError(f"branch/base need a git repository; {directory} is not one.")
            return ResolvedWorkspace(
                mode=mode or WorkspaceMode.IN_PLACE,
                directory=directory,
                requested_directory=directory,
            )
        toplevel = str(Path(toplevel).resolve())
        repo_root = await self._repo_root(directory, toplevel)
        notes: list[str] = []
        if mode == WorkspaceMode.IN_PLACE and Path(toplevel).is_relative_to(self.root):
            # Writing in a managed worktree needs its ownership, so in_place there is
            # the same as worktree mode on that worktree's branch.
            mode = WorkspaceMode.WORKTREE
            notes.append(
                "The directory is a coordinator-managed worktree, so it runs in worktree "
                "mode (single owner) instead of in_place."
            )
        if mode in (WorkspaceMode.READONLY, WorkspaceMode.IN_PLACE):
            return ResolvedWorkspace(
                mode=mode,
                directory=directory,
                requested_directory=directory,
                is_git=True,
                repo_root=repo_root,
                checkout_root=toplevel,
            )
        async with self._resolve_lock:
            resolved = await self._resolve_worktree(
                task_id=task_id,
                directory=directory,
                toplevel=toplevel,
                repo_root=repo_root,
                request=request,
                is_task_active=is_task_active,
            )
        resolved.notes = notes + resolved.notes
        return resolved

    async def _repo_root(self, directory: str, toplevel: str) -> str:
        common = await git_inspect.git_output(
            directory, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        if common:
            common_path = Path(common).resolve()
            if common_path.name == ".git":
                return str(common_path.parent)
        return toplevel

    async def _resolve_worktree(
        self,
        *,
        task_id: str,
        directory: str,
        toplevel: str,
        repo_root: str,
        request: WorkspaceRequest,
        is_task_active: Callable[[str], Awaitable[bool]] | None,
    ) -> ResolvedWorkspace:
        branch = request.branch
        if branch is None and toplevel != repo_root:
            # A linked worktree as directory: continue on its branch (for a worktree
            # managed here that means reusing it).
            branch = await git_inspect.current_branch(toplevel)
        branch = branch or auto_branch_name(task_id)
        check = await git_inspect.run_git(repo_root, "check-ref-format", "--branch", branch)
        if not check.ok:
            raise WorkspaceError(f"invalid branch name '{branch}'")
        worktree = self.root / repo_key(repo_root) / branch_slug(branch)
        await self._claim(task_id, worktree, branch, is_task_active)
        try:
            resolved = await self._prepare_worktree(
                task_id=task_id,
                repo_root=repo_root,
                worktree=worktree,
                branch=branch,
                request=request,
            )
        except BaseException:
            await self.release(task_id)
            raise
        relative = Path(directory).resolve()
        with contextlib.suppress(ValueError):
            sub = relative.relative_to(toplevel)
            if str(sub) not in ("", ".") and (worktree / sub).is_dir():
                resolved.directory = str(worktree / sub)
        resolved.requested_directory = directory
        resolved.checkout_root = toplevel
        return resolved

    async def _prepare_worktree(
        self,
        *,
        task_id: str,
        repo_root: str,
        worktree: Path,
        branch: str,
        request: WorkspaceRequest,
    ) -> ResolvedWorkspace:
        entries = await git_inspect.worktree_list(repo_root)
        if entries is None:
            raise WorkspaceError(f"could not list git worktrees of {repo_root}")
        registered = {str(Path(e["worktree"]).resolve()): e for e in entries if "worktree" in e}
        state = self._read_state(worktree)
        if str(worktree) in registered:
            on_branch = registered[str(worktree)].get("branch", "").removeprefix("refs/heads/")
            notes = []
            if on_branch != branch:
                notes.append(
                    f"The worktree is currently on {on_branch or 'a detached HEAD'}, not on "
                    f"{branch}."
                )
            self._write_state(
                worktree,
                {
                    **state,
                    "branch": branch,
                    "repo_root": repo_root,
                    "worktree": str(worktree),
                    "last_used_at": _now().isoformat(),
                    "last_task_id": task_id,
                },
            )
            logger.info(
                "subagent.workspace.worktree_reused",
                task_id=task_id,
                branch=branch,
                worktree=str(worktree),
            )
            return ResolvedWorkspace(
                mode=WorkspaceMode.WORKTREE,
                directory=str(worktree),
                requested_directory=str(worktree),
                is_git=True,
                repo_root=repo_root,
                branch=branch,
                base=state.get("base"),
                base_sha=state.get("base_sha"),
                worktree=str(worktree),
                notes=notes,
            )
        if worktree.exists():
            await git_inspect.worktree_prune(repo_root)
            raise WorkspaceError(
                f"{worktree} exists but is not a git worktree of {repo_root}; "
                "remove it by hand or use another branch"
            )
        for path, entry in registered.items():
            if entry.get("branch") == f"refs/heads/{branch}":
                raise WorkspaceError(
                    f"branch '{branch}' is already checked out at {path} (not a worktree "
                    "managed by the coordinator). Use another branch, or workspace_mode "
                    f"'in_place' with directory {path}."
                )
        # A new worktree is about to be created: clean up stale ones first.
        await self._reconcile_locked(prune=True)
        return await self._create_worktree(
            task_id=task_id,
            repo_root=repo_root,
            worktree=worktree,
            branch=branch,
            requested_base=request.base,
        )

    async def _create_worktree(
        self,
        *,
        task_id: str,
        repo_root: str,
        worktree: Path,
        branch: str,
        requested_base: str | None,
    ) -> ResolvedWorkspace:
        notes: list[str] = []
        remotes_out = await git_inspect.git_output(repo_root, "remote")
        remotes = set((remotes_out or "").split())
        base = requested_base or self._default_base or await self._auto_base(repo_root, remotes)
        if base is None:
            raise WorkspaceError(
                f"could not determine a base branch for {repo_root}; pass base explicitly"
            )
        remote, _, remote_branch = base.partition("/")
        fetch_failed = False
        if remote in remotes and remote_branch:
            fetched = await git_inspect.run_git(
                repo_root,
                "fetch",
                remote,
                "--end-of-options",
                _fetch_refspec(remote, remote_branch),
                timeout=git_inspect.GIT_FETCH_TIMEOUT_SECONDS,
            )
            if not fetched.ok:
                fetch_failed = True
                notes.append(f"git fetch of {base} failed, so the base may be stale.")
                logger.warning(
                    "subagent.workspace.fetch_failed",
                    task_id=task_id,
                    base=base,
                    error=fetched.error[:300],
                )
        base_sha = await git_inspect.resolve_ref(repo_root, base)
        if base_sha is None:
            raise WorkspaceError(f"base '{base}' does not resolve to a commit in {repo_root}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        origin = "origin" if "origin" in remotes else None
        if await git_inspect.branch_tip(repo_root, branch) is not None:
            args = ["worktree", "add", str(worktree), branch]
            how = f"attached existing local branch {branch}"
        else:
            remote_ref = None
            if origin is not None and not fetch_failed:
                await git_inspect.run_git(
                    repo_root,
                    "fetch",
                    origin,
                    "--end-of-options",
                    _fetch_refspec(origin, branch),
                    timeout=git_inspect.GIT_FETCH_TIMEOUT_SECONDS,
                )
                if await git_inspect.resolve_ref(repo_root, f"refs/remotes/{origin}/{branch}"):
                    remote_ref = f"{origin}/{branch}"
            if remote_ref is not None:
                args = ["worktree", "add", "--track", "-b", branch, str(worktree), remote_ref]
                how = f"created branch {branch} tracking {remote_ref}"
            else:
                args = ["worktree", "add", "--no-track", "-b", branch, str(worktree), base_sha]
                how = f"created branch {branch} from {base} ({base_sha[:12]})"
        added = await git_inspect.run_git(repo_root, *args)
        if not added.ok:
            raise WorkspaceError(f"git worktree add failed: {added.error[:500]}")
        now = _now().isoformat()
        self._write_state(
            worktree,
            {
                "branch": branch,
                "repo_root": repo_root,
                "worktree": str(worktree),
                "base": base,
                "base_sha": base_sha,
                "created_at": now,
                "last_used_at": now,
                "last_task_id": task_id,
            },
        )
        logger.info(
            "subagent.workspace.worktree_created",
            task_id=task_id,
            branch=branch,
            base=base,
            worktree=str(worktree),
            how=how,
        )
        return ResolvedWorkspace(
            mode=WorkspaceMode.WORKTREE,
            directory=str(worktree),
            requested_directory=str(worktree),
            is_git=True,
            repo_root=repo_root,
            branch=branch,
            base=base,
            base_sha=base_sha,
            worktree=str(worktree),
            worktree_created=True,
            notes=[how[0].upper() + how[1:] + ".", *notes],
        )

    async def _auto_base(self, repo_root: str, remotes: set[str]) -> str | None:
        if "origin" in remotes:
            head = await git_inspect.git_output(
                repo_root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"
            )
            if head and head.startswith("refs/remotes/"):
                return head.removeprefix("refs/remotes/")
        for candidate in _AUTO_BASE_CANDIDATES:
            if await git_inspect.resolve_ref(repo_root, candidate):
                return candidate
        return None

    # ------------------------------------------------------------------ ownership

    def _lock_path(self, worktree: Path) -> Path:
        return self._state_dir / worktree.parent.name / f"{worktree.name}.lock"

    def _state_path(self, worktree: Path) -> Path:
        return self._state_dir / worktree.parent.name / f"{worktree.name}.json"

    def _read_state(self, worktree: Path) -> dict[str, Any]:
        return _read_holder(self._state_path(worktree))

    def _write_state(self, worktree: Path, state: dict[str, Any]) -> None:
        path = self._state_path(worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    async def _claim(
        self,
        task_id: str,
        worktree: Path,
        branch: str,
        is_task_active: Callable[[str], Awaitable[bool]] | None,
    ) -> None:
        key = str(worktree)
        current = self._owners.get(key)
        if current is not None:
            if current.task_id == task_id:
                return
            stale = is_task_active is not None and not await is_task_active(current.task_id)
            if not stale:
                raise WorkspaceError(
                    f"the worktree for branch '{branch}' ({worktree}) is in use by task "
                    f"{current.task_id}. A worktree has one writer at a time and the task "
                    "is not queued: wait for that task to finish (or cancel it), or use "
                    "another branch.",
                    retryable=True,
                )
            logger.warning(
                "subagent.workspace.stale_owner_released",
                worktree=key,
                stale_task_id=current.task_id,
            )
            self._release_key(key)
        handle = _try_lock(self._lock_path(worktree))
        if handle is None:
            holder = _read_holder(self._lock_path(worktree))
            raise WorkspaceError(
                f"the worktree for branch '{branch}' ({worktree}) is in use by task "
                f"{_holder_text(holder)} of another instance. The task is not queued: "
                "wait for it to finish, or use another branch.",
                retryable=True,
            )
        _write_holder(
            handle,
            {
                "task_id": task_id,
                "instance": self._instance,
                "pid": os.getpid(),
                "since": _now().isoformat(),
            },
        )
        self._owners[key] = _Ownership(task_id=task_id, handle=handle)

    async def ensure_owned(self, task_id: str, workspace: ResolvedWorkspace) -> None:
        """Before a run: make sure ``task_id`` (still) owns its worktree.

        Ownership is normally taken at enqueue; after a restart a pending task re-takes
        it here. Raises WorkspaceError when the worktree is gone or owned by another task.
        """
        if workspace.mode != WorkspaceMode.WORKTREE or workspace.worktree is None:
            return
        worktree = Path(workspace.worktree)
        owner = self._owners.get(str(worktree))
        if owner is None or owner.task_id != task_id:
            await self._claim(task_id, worktree, workspace.branch or "?", None)
        if not Path(workspace.directory).is_dir():
            await self.release(task_id)
            raise WorkspaceError(f"the task's worktree {workspace.directory} no longer exists")

    async def release(self, task_id: str) -> None:
        """Drop every worktree ownership of ``task_id``; records the last use time."""
        for key in [k for k, o in self._owners.items() if o.task_id == task_id]:
            worktree = Path(key)
            state = self._read_state(worktree)
            if state:
                state["last_used_at"] = _now().isoformat()
                with contextlib.suppress(OSError):
                    self._write_state(worktree, state)
            self._release_key(key)
            logger.info("subagent.workspace.released", task_id=task_id, worktree=key)

    def _release_key(self, key: str) -> None:
        owner = self._owners.pop(key, None)
        if owner is not None:
            _unlock(owner.handle)

    def owner_of(self, worktree: str) -> str | None:
        owner = self._owners.get(str(Path(worktree).resolve()))
        return owner.task_id if owner is not None else None

    # ------------------------------------------------------------------ in_place lock

    def _in_place_lock_path(self, checkout_root: str) -> Path:
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(checkout_root).name) or "checkout"
        return self._in_place_lock_dir / f"{name}-{_short_hash(checkout_root, 16)}.lock"

    @contextlib.asynccontextmanager
    async def in_place_lock(
        self,
        task_id: str,
        workspace: ResolvedWorkspace,
        *,
        timeout: float | None = None,
        on_wait: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        """Hold the checkout lock of an in_place git workspace (no-op otherwise).

        Waits (polling) while another task holds it; WorkspaceError after ``timeout``.
        """
        if workspace.mode != WorkspaceMode.IN_PLACE or workspace.checkout_root is None:
            yield
            return
        path = self._in_place_lock_path(workspace.checkout_root)
        limit = self.in_place_lock_timeout if timeout is None else timeout
        deadline = asyncio.get_running_loop().time() + limit
        waited = False
        while True:
            handle = _try_lock(path)
            if handle is not None:
                break
            if not waited:
                waited = True
                holder = _read_holder(path)
                logger.info(
                    "subagent.workspace.in_place_waiting",
                    task_id=task_id,
                    checkout=workspace.checkout_root,
                    holder=holder,
                )
                if on_wait is not None:
                    await on_wait(holder)
            if asyncio.get_running_loop().time() >= deadline:
                holder = _read_holder(path)
                raise WorkspaceError(
                    f"in_place lock on {workspace.checkout_root} is held by task "
                    f"{_holder_text(holder)}; gave up after {int(limit)}s"
                )
            await asyncio.sleep(self._lock_poll_seconds)
        _write_holder(
            handle,
            {
                "task_id": task_id,
                "instance": self._instance,
                "pid": os.getpid(),
                "since": _now().isoformat(),
            },
        )
        logger.info(
            "subagent.workspace.in_place_locked",
            task_id=task_id,
            checkout=workspace.checkout_root,
        )
        try:
            yield
        finally:
            _unlock(handle)
            logger.info(
                "subagent.workspace.in_place_unlocked",
                task_id=task_id,
                checkout=workspace.checkout_root,
            )

    # ------------------------------------------------------------------ cleanup

    async def reconcile(self, *, prune: bool = True) -> list[WorktreeCleanupReport]:
        """Prune and clean up managed worktrees; see the module docstring for the rules."""
        async with self._resolve_lock:
            return await self._reconcile_locked(prune=prune)

    async def _reconcile_locked(self, *, prune: bool) -> list[WorktreeCleanupReport]:
        states: list[tuple[Path, dict[str, Any]]] = []
        if self._state_dir.is_dir():
            for state_path in sorted(self._state_dir.glob("*/*.json")):
                state = _read_holder(state_path)
                worktree = self.root / state_path.parent.name / state_path.stem
                states.append((worktree, state))
        if prune:
            for repo in sorted({str(s.get("repo_root")) for _, s in states if s.get("repo_root")}):
                await git_inspect.worktree_prune(repo)
        reports: list[WorktreeCleanupReport] = []
        gh_available = True
        for worktree, state in states:
            report, gh_available = await self._reconcile_one(worktree, state, gh_available)
            reports.append(report)
            log = logger.info if report.action != "kept" else logger.debug
            log(
                "subagent.workspace.cleanup",
                worktree=report.worktree,
                branch=report.branch,
                action=report.action,
                reason=report.reason,
            )
        kept = [r for r in reports if r.action == "kept"]
        if reports:
            logger.info(
                "subagent.workspace.reconciled",
                total=len(reports),
                removed=sum(1 for r in reports if r.action == "removed"),
                kept=len(kept),
                gh_available=gh_available,
            )
        return reports

    async def _reconcile_one(
        self, worktree: Path, state: dict[str, Any], gh_available: bool
    ) -> tuple[WorktreeCleanupReport, bool]:
        branch = str(state.get("branch") or "")
        repo_root = state.get("repo_root")

        def report(action: str, reason: str) -> WorktreeCleanupReport:
            return WorktreeCleanupReport(str(worktree), branch, action, reason)

        if not isinstance(repo_root, str) or not branch:
            return report("kept", "incomplete state file"), gh_available
        recorded = state.get("worktree")
        if recorded is not None and Path(str(recorded)) != worktree:
            # Only ever touch the worktree at the managed path, whatever the file says.
            return report("kept", "state does not match the managed path"), gh_available
        if str(worktree) in self._owners:
            return report("kept", "active owner task"), gh_available
        entries = await git_inspect.worktree_list(repo_root)
        if entries is None:
            return report("kept", "could not list worktrees"), gh_available
        registered = {str(Path(e["worktree"]).resolve()) for e in entries if "worktree" in e}
        if str(worktree) not in registered:
            if worktree.exists():
                return report("kept", "directory is not a registered worktree"), gh_available
            self._forget(worktree)
            return report("forgotten", "worktree no longer exists"), gh_available
        handle = _try_lock(self._lock_path(worktree))
        if handle is None:
            return report("kept", "active owner task (another instance)"), gh_available
        try:
            pr: dict[str, Any] | None = None
            if gh_available:
                pr, gh_available = await self._pr_state(repo_root, branch)
            pr_state = pr.get("state") if pr else None
            if pr_state == "OPEN":
                return report("kept", "PR open"), gh_available
            last_used = _parse_time(state.get("last_used_at") or state.get("created_at"))
            if last_used is None:
                last_used = datetime.fromtimestamp(worktree.stat().st_mtime, UTC)
            expired = _now() - last_used > self.ttl
            if pr_state not in ("MERGED", "CLOSED") and not expired:
                return report("kept", "used within the TTL"), gh_available
            dirty = await git_inspect.has_uncommitted_changes(str(worktree))
            if dirty is None or dirty:
                return report("kept", "uncommitted changes"), gh_available
            if await self._has_unpushed(str(worktree), "HEAD", pr):
                return report("kept", "unpushed commits"), gh_available
            if not await git_inspect.worktree_remove_clean(repo_root, str(worktree)):
                return report("kept", "git worktree remove failed"), gh_available
            self._forget(worktree)
            why = f"PR {pr_state.lower()}" if pr_state in ("MERGED", "CLOSED") else "TTL expired"
            if pr_state == "MERGED" and self._delete_merged_branches:
                # The worktree may have left the branch, so check the branch tip itself.
                if await self._has_unpushed(repo_root, f"refs/heads/{branch}", pr):
                    why += ", branch kept: its tip is not verified as pushed"
                elif await git_inspect.branch_delete(repo_root, branch):
                    why += ", merged branch deleted"
            return report("removed", why), gh_available
        finally:
            _unlock(handle)

    def _forget(self, worktree: Path) -> None:
        with contextlib.suppress(OSError):
            self._state_path(worktree).unlink()

    async def _pr_state(self, repo_root: str, branch: str) -> tuple[dict[str, Any] | None, bool]:
        """(latest relevant PR of ``branch`` or None, whether gh is usable)."""
        code, out = await self._gh_runner(
            repo_root,
            [
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,state,headRefOid",
                "--limit",
                "20",
            ],
        )
        if code != 0:
            logger.info("subagent.workspace.gh_unavailable", code=code, output=out[:300])
            return None, False
        try:
            prs = json.loads(out or "[]")
        except ValueError:
            return None, False
        if not isinstance(prs, list) or not prs:
            return None, True
        by_state = {str(p.get("state")): p for p in prs if isinstance(p, dict)}
        for wanted in ("OPEN", "MERGED", "CLOSED"):
            if wanted in by_state:
                return by_state[wanted], True
        return None, True

    @staticmethod
    async def _has_unpushed(path: str, ref: str, pr: dict[str, Any] | None) -> bool:
        """Commits of ``ref`` that no remote branch (nor the PR head) contains; True if unknown."""
        if pr and isinstance(pr.get("headRefOid"), str):
            contained = await git_inspect.run_git(
                path, "merge-base", "--is-ancestor", ref, pr["headRefOid"]
            )
            if contained.ok:
                return False
        out = await git_inspect.git_output(path, "rev-list", "--count", ref, "--not", "--remotes")
        if out is None:
            return True
        try:
            return int(out) > 0
        except ValueError:
            return True


def _fetch_refspec(remote: str, branch: str) -> str:
    """Fetch only ``branch`` into its remote-tracking ref, never into a local branch."""
    return f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _run_gh(cwd: str, args: list[str]) -> tuple[int, str]:
    """Run ``gh`` in ``cwd``: (exit code, stdout or stderr); -1 when gh cannot run."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GH_TIMEOUT_SECONDS)
    except (OSError, TimeoutError) as exc:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        return -1, f"{type(exc).__name__}: {exc}"
    text = (out if proc.returncode == 0 else err).decode("utf-8", errors="replace").strip()
    return proc.returncode if proc.returncode is not None else -1, text


def task_workspace_info(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Caller-facing view of a task's stored ``metadata["workspace"]``; None if absent."""
    raw = metadata.get("workspace")
    if not isinstance(raw, dict):
        return None
    try:
        workspace = ResolvedWorkspace.model_validate(raw)
    except ValueError:
        return None
    return workspace.info().model_dump(mode="json")


def environment_block(workspace: ResolvedWorkspace) -> str:
    """Code-generated workspace description prepended to the sub-agent's objective."""
    lines = ["--- Environment (set by the coordinator) ---", f"Workspace mode: {workspace.mode}"]
    if workspace.mode == WorkspaceMode.WORKTREE:
        lines += [
            f"Repository: {workspace.repo_root}",
            f"Branch: {workspace.branch}",
            f"Working directory: {workspace.directory}",
            f"It is a dedicated git worktree for branch {workspace.branch}, managed by "
            "the coordinator; you are its only writer for this run.",
        ]
        if workspace.base:
            lines.append(f"Base: {workspace.base} ({(workspace.base_sha or '')[:12]})")
        lines.append(
            "This worktree was just created: dependencies (e.g. .venv) are not installed yet."
            if workspace.worktree_created
            else "This worktree was reused from an earlier task on this branch: keep its "
            "existing state (.venv, uncommitted changes) in mind."
        )
        lines += workspace.notes
        lines += [
            "Rules:",
            f"- Make every change inside {workspace.directory} and commit on branch "
            f"{workspace.branch}. Do not create or switch branches.",
            f"- Do not modify the primary checkout {workspace.repo_root} or other "
            "worktrees. Paths into the primary checkout in the task mean the same "
            "relative paths inside your worktree.",
            "- Do not remove the worktree; the coordinator cleans it up after the PR is "
            "merged or closed.",
        ]
    elif workspace.mode == WorkspaceMode.IN_PLACE:
        lines.append(f"Working directory: {workspace.directory}")
        if workspace.checkout_root is not None:
            lines += [
                "You work directly in this directory (the coordinator made no worktree "
                "for this task). An exclusive "
                f"lock on {workspace.checkout_root} is held for your run, so no other "
                "in_place task works here at the same time.",
                "Keep changes to what the task asks (e.g. a fast-forward after a merge, "
                "local config edits).",
            ]
        else:
            lines.append("It is not a git repository.")
    else:
        lines += [
            f"Working directory: {workspace.directory}",
            "Read-only task: do not modify, create or delete files, do not commit, and "
            "do not run git commands that change state (checkout, merge, pull, reset, "
            "stash, ...). Report your findings instead.",
        ]
    lines.append("--- Task ---")
    return "\n".join(lines)
