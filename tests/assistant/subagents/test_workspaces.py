"""Tests for sub-agent workspaces: worktree / readonly / in_place modes and cleanup."""

import asyncio
import json
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolDefaultParams,
    ToolDefinition,
    ToolsConfig,
)
from assistant.store.facade import StoreFacade
from assistant.store.models import TaskStatus
from assistant.subagents.contracts import DelegationResult, DelegationRun
from assistant.subagents.coordinator import DelegationCoordinator
from assistant.subagents.interfaces import DelegationBackendAdapterInterface
from assistant.subagents.workspaces import (
    ResolvedWorkspace,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceMode,
    WorkspaceRequest,
    branch_slug,
    environment_block,
)

GhRunner = Callable[[str, list[str]], Awaitable[tuple[int, str]]]


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "t")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "t@example.com")


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def commit(cwd: Path, name: str, content: str = "x\n") -> str:
    (cwd / name).write_text(content)
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", f"add {name}")
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A primary checkout cloned from a bare origin with a ``master`` branch."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "master")
    (seed / ".gitignore").write_text(".venv/\n")
    commit(seed, "README.md", "hello\n")
    (seed / "pkg").mkdir()
    commit(seed, "pkg/mod.py", "x = 1\n")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    return checkout


def push_from_other_clone(tmp_path: Path, name: str, branch: str = "master") -> str:
    """Advance ``branch`` on origin from a second clone; returns the new sha."""
    other = tmp_path / f"other-{name.replace('/', '-')}"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    if branch != "master":
        git(other, "checkout", "-q", "-b", branch)
    sha = commit(other, name)
    git(other, "push", "-q", "origin", branch)
    return sha


def make_config(data_root: Path, **defaults: Any) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root=str(data_root), timezone="UTC", instance_name="test"),
        telegram=TelegramChannelConfig(enabled=False, bot_token="", allowlist=[]),
        model=ModelConfig(
            default_model_id="claude-sonnet-4-5", model_allowlist=["claude-sonnet-4-5"]
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=[], denied_capabilities=[]),
        tools=ToolsConfig(
            tools=[
                ToolDefinition(
                    tool_id="delegate_subagent_task",
                    entrypoint="assistant.agent.tools.delegate_subagent_task:delegate_subagent_task",
                    default_params=ToolDefaultParams(**defaults),
                )
            ]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def gh_unavailable() -> GhRunner:
    async def _run(cwd: str, args: list[str]) -> tuple[int, str]:
        return -1, "FileNotFoundError: gh"

    return _run


def gh_with(prs_by_branch: dict[str, list[dict[str, Any]]]) -> GhRunner:
    async def _run(cwd: str, args: list[str]) -> tuple[int, str]:
        branch = args[args.index("--head") + 1]
        return 0, json.dumps(prs_by_branch.get(branch, []))

    return _run


def manager(tmp_path: Path, gh: GhRunner | None = None, **defaults: Any) -> WorkspaceManager:
    return WorkspaceManager(
        make_config(tmp_path / "data", **defaults),
        gh_runner=gh or gh_unavailable(),
        lock_poll_seconds=0.02,
    )


async def resolve(
    mgr: WorkspaceManager, task_id: str, directory: Path | str, **request: Any
) -> ResolvedWorkspace:
    return await mgr.resolve(
        task_id=task_id,
        directory=str(directory),
        request=WorkspaceRequest.parse(request or None),
    )


def age_state(mgr: WorkspaceManager, workspace: ResolvedWorkspace, days: int) -> None:
    assert workspace.worktree is not None
    worktree = Path(workspace.worktree)
    path = mgr.root / ".state" / worktree.parent.name / f"{worktree.name}.json"
    state = json.loads(path.read_text())
    state["last_used_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    path.write_text(json.dumps(state))


# --------------------------------------------------------------------------- creation


@pytest.mark.asyncio
async def test_worktree_created_from_freshly_fetched_base(tmp_path: Path, repo: Path) -> None:
    new_master = push_from_other_clone(tmp_path, "upstream.txt")
    assert git(repo, "rev-parse", "origin/master") != new_master  # checkout is stale
    mgr = manager(tmp_path)

    ws = await resolve(mgr, "t1", repo, branch="feat/issue-80")

    assert ws.mode == WorkspaceMode.WORKTREE
    assert ws.worktree_created is True
    assert ws.branch == "feat/issue-80"
    assert ws.base == "origin/master"
    assert ws.base_sha == new_master
    worktree = Path(ws.directory)
    assert worktree.parent.parent == mgr.root
    assert worktree.name == branch_slug("feat/issue-80")
    assert git(worktree, "rev-parse", "HEAD") == new_master
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == "feat/issue-80"
    # A new branch must not track the base (a plain `git push` must not go to master).
    upstream = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "@{u}"], capture_output=True
    )
    assert upstream.returncode != 0
    # The primary checkout is untouched.
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "master"
    assert mgr.owner_of(ws.directory) == "t1"


@pytest.mark.asyncio
async def test_explicit_base_and_default_base_config(tmp_path: Path, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "develop")
    develop_sha = commit(repo, "dev.txt")
    git(repo, "checkout", "-q", "master")
    mgr = manager(tmp_path)
    ws = await resolve(mgr, "t1", repo, branch="from-develop", base="develop")
    assert ws.base_sha == develop_sha

    mgr2 = manager(tmp_path, delegation_worktree_default_base="develop")
    ws2 = await resolve(mgr2, "t2", repo, branch="from-config")
    assert ws2.base == "develop"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base",
    ["origin/+master:refs/heads/victim", "origin/--upload-pack=touch {marker};false"],
)
async def test_base_cannot_act_as_refspec_or_option(tmp_path: Path, repo: Path, base: str) -> None:
    git(repo, "checkout", "-q", "-b", "victim")
    victim_sha = commit(repo, "local-only.txt")
    git(repo, "checkout", "-q", "master")
    push_from_other_clone(tmp_path, "upstream.txt")
    marker = tmp_path / "marker"
    mgr = manager(tmp_path)

    with pytest.raises(WorkspaceError, match="invalid base"):
        await resolve(mgr, "t1", repo, branch="feat/x", base=base.format(marker=marker))

    assert git(repo, "rev-parse", "refs/heads/victim") == victim_sha
    assert not marker.exists()
    assert len(git(repo, "worktree", "list").splitlines()) == 1  # nothing was created


@pytest.mark.asyncio
async def test_auto_branch_name_and_subdirectory(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    ws = await resolve(mgr, "dlg-abc", repo / "pkg")
    assert ws.branch == "subagent/dlg-abc"
    assert ws.worktree is not None
    assert ws.directory == str(Path(ws.worktree) / "pkg")
    assert ws.requested_directory == str(repo / "pkg")


@pytest.mark.asyncio
async def test_existing_local_and_remote_branches_are_attached(tmp_path: Path, repo: Path) -> None:
    remote_sha = push_from_other_clone(tmp_path, "remote.txt", branch="feat/remote")
    git(repo, "branch", "local-only", "origin/master")
    git(repo, "checkout", "-q", "local-only")
    local_sha = commit(repo, "local.txt")
    git(repo, "checkout", "-q", "master")
    mgr = manager(tmp_path)

    remote_ws = await resolve(mgr, "t1", repo, branch="feat/remote")
    assert git(Path(remote_ws.directory), "rev-parse", "HEAD") == remote_sha
    upstream = git(Path(remote_ws.directory), "rev-parse", "--abbrev-ref", "@{u}")
    assert upstream == "origin/feat/remote"

    local_ws = await resolve(mgr, "t2", repo, branch="local-only")
    assert git(Path(local_ws.directory), "rev-parse", "HEAD") == local_sha


@pytest.mark.asyncio
async def test_branch_checked_out_elsewhere_is_rejected(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    with pytest.raises(WorkspaceError, match="already checked out"):
        await resolve(mgr, "t1", repo, branch="master")
    assert mgr.owner_of(str(mgr.root)) is None
    assert not mgr._owners  # the claim is undone on failure


# --------------------------------------------------------------------------- reuse/ownership


@pytest.mark.asyncio
async def test_follow_up_task_reuses_worktree_with_its_state(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    first = await resolve(mgr, "implement", repo, branch="feat/x")
    worktree = Path(first.directory)
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "marker").write_text("installed")
    (worktree / "README.md").write_text("uncommitted edit\n")
    await mgr.release("implement")

    second = await resolve(mgr, "review", repo, branch="feat/x")

    assert second.directory == first.directory
    assert second.worktree_created is False
    assert (worktree / ".venv" / "marker").read_text() == "installed"
    assert (worktree / "README.md").read_text() == "uncommitted edit\n"
    assert "reused" in environment_block(second)
    # Passing the worktree itself as directory continues on its branch.
    await mgr.release("review")
    third = await resolve(mgr, "fix", worktree)
    assert third.directory == first.directory
    assert third.branch == "feat/x"


@pytest.mark.asyncio
async def test_second_writer_on_same_branch_is_rejected(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    await resolve(mgr, "t1", repo, branch="feat/x")
    with pytest.raises(WorkspaceError, match="in use by task t1") as exc_info:
        await resolve(mgr, "t2", repo, branch="feat/x")
    assert exc_info.value.retryable is True
    await mgr.release("t1")
    ws = await resolve(mgr, "t2", repo, branch="feat/x")
    assert mgr.owner_of(ws.directory) == "t2"


@pytest.mark.asyncio
async def test_owner_in_another_instance_is_rejected(tmp_path: Path, repo: Path) -> None:
    instance_a = manager(tmp_path)
    instance_b = manager(tmp_path)  # same shared root, separate process state
    await resolve(instance_a, "a1", repo, branch="feat/x")
    with pytest.raises(WorkspaceError, match="another instance"):
        await resolve(instance_b, "b1", repo, branch="feat/x")
    await instance_a.release("a1")
    await resolve(instance_b, "b1", repo, branch="feat/x")


@pytest.mark.asyncio
async def test_stale_in_process_owner_is_released(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    await resolve(mgr, "ended", repo, branch="feat/x")

    async def is_active(task_id: str) -> bool:
        return False

    ws = await mgr.resolve(
        task_id="next",
        directory=str(repo),
        request=WorkspaceRequest(branch="feat/x"),
        is_task_active=is_active,
    )
    assert mgr.owner_of(ws.directory) == "next"


@pytest.mark.asyncio
async def test_different_branches_get_different_worktrees(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    a, b = await asyncio.gather(
        resolve(mgr, "t1", repo, branch="feat/a"), resolve(mgr, "t2", repo, branch="feat/b")
    )
    assert a.directory != b.directory
    assert mgr.owner_of(a.directory) == "t1"
    assert mgr.owner_of(b.directory) == "t2"
    assert git(Path(a.directory), "symbolic-ref", "--short", "HEAD") == "feat/a"
    assert git(Path(b.directory), "symbolic-ref", "--short", "HEAD") == "feat/b"


# --------------------------------------------------------------------------- mode validation


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"mode": "sandbox"}, "unknown workspace_mode"),
        ({"mode": "readonly", "branch": "x"}, "only to workspace_mode 'worktree'"),
        ({"mode": "in_place", "base": "origin/master"}, "only to workspace_mode 'worktree'"),
        ({"mode": 3}, "must be a string"),
        ("worktree", "must be an object"),
        ({"base": "origin/+master:refs/heads/victim"}, "invalid base"),
        ({"base": "origin/--upload-pack=touch x"}, "invalid base"),
        ({"base": "-master"}, "invalid base"),
        ({"base": "origin/a..b"}, "invalid base"),
        ({"base": "origin/master^{tree}"}, "invalid base"),
    ],
)
def test_workspace_request_validation(raw: object, message: str) -> None:
    with pytest.raises(WorkspaceError, match=message):
        WorkspaceRequest.parse(raw)


def test_workspace_request_blank_values_are_unset() -> None:
    assert WorkspaceRequest.parse({"mode": " ", "branch": "", "base": None}).is_empty


@pytest.mark.asyncio
async def test_non_git_directory_modes(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    mgr = manager(tmp_path)
    default = await resolve(mgr, "t1", plain)
    assert default.mode == WorkspaceMode.IN_PLACE
    assert default.directory == str(plain)
    assert default.checkout_root is None  # no lock for a non-git directory
    readonly = await resolve(mgr, "t2", plain, mode="readonly")
    assert readonly.mode == WorkspaceMode.READONLY
    with pytest.raises(WorkspaceError, match="needs a git repository"):
        await resolve(mgr, "t3", plain, mode="worktree")
    with pytest.raises(WorkspaceError, match="need a git repository"):
        await resolve(mgr, "t4", plain, branch="feat/x")


@pytest.mark.asyncio
async def test_readonly_and_in_place_use_directory_as_given(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    readonly = await resolve(mgr, "t1", repo, mode="readonly")
    in_place = await resolve(mgr, "t2", repo, mode="in_place")
    for ws in (readonly, in_place):
        assert ws.directory == str(repo)
        assert ws.worktree is None
    assert in_place.checkout_root == str(repo.resolve())
    assert not mgr._owners
    assert "Read-only task" in environment_block(readonly)
    assert "exclusive lock" in environment_block(in_place)


@pytest.mark.asyncio
async def test_in_place_on_managed_worktree_needs_ownership(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    owned = await resolve(mgr, "t1", repo, branch="feat/x")
    with pytest.raises(WorkspaceError, match="in use by task t1"):
        await resolve(mgr, "t2", owned.directory, mode="in_place")
    await mgr.release("t1")
    ws = await resolve(mgr, "t2", owned.directory, mode="in_place")
    assert ws.mode == WorkspaceMode.WORKTREE
    assert ws.directory == owned.directory


# --------------------------------------------------------------------------- in_place lock


@pytest.mark.asyncio
async def test_in_place_lock_serializes_across_instances(tmp_path: Path, repo: Path) -> None:
    instance_a = manager(tmp_path)
    instance_b = manager(tmp_path)
    ws_a = await resolve(instance_a, "a1", repo, mode="in_place")
    ws_b = await resolve(instance_b, "b1", repo / "pkg", mode="in_place")  # same checkout
    order: list[str] = []
    waited: list[dict[str, Any]] = []

    async def on_wait(holder: dict[str, Any]) -> None:
        waited.append(holder)

    async with instance_a.in_place_lock("a1", ws_a):
        with pytest.raises(WorkspaceError, match="held by task a1"):
            async with instance_b.in_place_lock("b1", ws_b, timeout=0.1):
                pass  # pragma: no cover

        async def second() -> None:
            async with instance_b.in_place_lock("b1", ws_b, timeout=5, on_wait=on_wait):
                order.append("b")

        waiter = asyncio.create_task(second())
        await asyncio.sleep(0.1)
        order.append("a")
    await asyncio.wait_for(waiter, timeout=5)
    assert order == ["a", "b"]
    assert waited and waited[0]["task_id"] == "a1"


@pytest.mark.asyncio
async def test_in_place_lock_is_per_checkout(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    other = tmp_path / "other-repo"
    other.mkdir()
    git(other, "init", "-q", "-b", "master")
    ws_repo = await resolve(mgr, "t1", repo, mode="in_place")
    ws_other = await resolve(mgr, "t2", other, mode="in_place")
    async with mgr.in_place_lock("t1", ws_repo), mgr.in_place_lock("t2", ws_other, timeout=0.1):
        pass


# --------------------------------------------------------------------------- cleanup


async def _pushed_worktree(
    mgr: WorkspaceManager, task_id: str, repo: Path, branch: str
) -> tuple[ResolvedWorkspace, str]:
    ws = await resolve(mgr, task_id, repo, branch=branch)
    worktree = Path(ws.directory)
    sha = commit(worktree, f"{branch.replace('/', '-')}.txt")
    git(worktree, "push", "-q", "origin", branch)
    await mgr.release(task_id)
    return ws, sha


@pytest.mark.asyncio
async def test_merged_pr_worktree_removed_and_branch_deleted(tmp_path: Path, repo: Path) -> None:
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/merged")
    prs["feat/merged"] = [{"number": 1, "state": "MERGED", "headRefOid": sha}]

    reports = await mgr.reconcile()

    assert [(r.action, r.reason) for r in reports] == [
        ("removed", "PR merged, merged branch deleted")
    ]
    assert not Path(ws.directory).exists()
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/feat/merged"],
            capture_output=True,
        ).returncode
        != 0
    )
    assert await mgr.reconcile() == []  # its state is gone too


@pytest.mark.asyncio
async def test_merged_pr_keeps_branch_with_unpushed_commits_off_head(
    tmp_path: Path, repo: Path
) -> None:
    """The worktree left its branch: HEAD is pushed, but the branch tip has local work."""
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/diverged")
    worktree = Path(ws.directory)
    local_only = commit(worktree, "local-only.txt")  # on the branch, never pushed
    git(worktree, "checkout", "-q", "--detach", sha)
    prs["feat/diverged"] = [{"number": 9, "state": "MERGED", "headRefOid": sha}]

    reports = await mgr.reconcile()

    assert [(r.action, r.reason) for r in reports] == [
        ("removed", "PR merged, branch kept: its tip is not verified as pushed")
    ]
    assert not worktree.exists()
    assert git(repo, "rev-parse", "refs/heads/feat/diverged") == local_only


@pytest.mark.asyncio
async def test_closed_pr_removes_worktree_but_keeps_branch(tmp_path: Path, repo: Path) -> None:
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/closed")
    prs["feat/closed"] = [{"number": 2, "state": "CLOSED", "headRefOid": sha}]
    reports = await mgr.reconcile()
    assert [r.action for r in reports] == ["removed"]
    assert not Path(ws.directory).exists()
    assert git(repo, "rev-parse", "refs/heads/feat/closed") == sha


@pytest.mark.asyncio
async def test_squash_merged_pr_with_deleted_remote_branch(tmp_path: Path, repo: Path) -> None:
    """After a squash merge the remote branch is gone; the PR head still proves the push."""
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/squash")
    git(repo, "push", "-q", "origin", "--delete", "feat/squash")
    git(repo, "fetch", "-q", "--prune", "origin")
    prs["feat/squash"] = [{"number": 3, "state": "MERGED", "headRefOid": sha}]
    assert [r.action for r in await mgr.reconcile()] == ["removed"]
    assert not Path(ws.directory).exists()


@pytest.mark.asyncio
async def test_open_pr_and_recent_worktrees_are_kept(tmp_path: Path, repo: Path) -> None:
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    open_ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/open")
    prs["feat/open"] = [
        {"number": 4, "state": "CLOSED", "headRefOid": sha},
        {"number": 5, "state": "OPEN", "headRefOid": sha},
    ]
    recent_ws, _ = await _pushed_worktree(mgr, "t2", repo, "feat/no-pr")
    reports = {r.branch: (r.action, r.reason) for r in await mgr.reconcile()}
    assert reports == {
        "feat/open": ("kept", "PR open"),
        "feat/no-pr": ("kept", "used within the TTL"),
    }
    assert Path(open_ws.directory).exists() and Path(recent_ws.directory).exists()


@pytest.mark.asyncio
async def test_open_pr_keeps_worktree_after_ttl(tmp_path: Path, repo: Path) -> None:
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs), delegation_worktree_ttl_days=7)
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/in-review")
    prs["feat/in-review"] = [{"number": 10, "state": "OPEN", "headRefOid": sha}]
    age_state(mgr, ws, days=30)

    assert [(r.action, r.reason) for r in await mgr.reconcile()] == [("kept", "PR open")]
    assert Path(ws.directory).exists()


@pytest.mark.asyncio
async def test_uncommitted_changes_block_cleanup(tmp_path: Path, repo: Path) -> None:
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/dirty")
    prs["feat/dirty"] = [{"number": 6, "state": "MERGED", "headRefOid": sha}]
    (Path(ws.directory) / "README.md").write_text("local edit\n")
    assert [(r.action, r.reason) for r in await mgr.reconcile()] == [
        ("kept", "uncommitted changes")
    ]
    (Path(ws.directory) / "README.md").write_text("hello\n")
    (Path(ws.directory) / "new-untracked.txt").write_text("x")
    assert [r.reason for r in await mgr.reconcile()] == ["uncommitted changes"]
    assert Path(ws.directory).exists()


@pytest.mark.asyncio
async def test_untracked_file_blocks_ttl_cleanup_whatever_git_config(
    tmp_path: Path, repo: Path
) -> None:
    git(repo, "config", "status.showUntrackedFiles", "no")
    mgr = manager(tmp_path)
    ws = await resolve(mgr, "t1", repo, branch="feat/precious")
    precious = Path(ws.directory) / "precious.txt"
    precious.write_text("x")
    await mgr.release("t1")
    age_state(mgr, ws, days=30)

    assert [(r.action, r.reason) for r in await mgr.reconcile()] == [
        ("kept", "uncommitted changes")
    ]
    assert precious.exists()


@pytest.mark.asyncio
async def test_unpushed_commits_block_cleanup(tmp_path: Path, repo: Path) -> None:
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/unpushed")
    commit(Path(ws.directory), "later.txt")  # committed after the PR head, never pushed
    prs["feat/unpushed"] = [{"number": 7, "state": "MERGED", "headRefOid": sha}]
    assert [(r.action, r.reason) for r in await mgr.reconcile()] == [("kept", "unpushed commits")]
    assert Path(ws.directory).exists()


@pytest.mark.asyncio
async def test_active_owner_blocks_cleanup(tmp_path: Path, repo: Path) -> None:
    prs: dict[str, list[dict[str, Any]]] = {}
    mgr = manager(tmp_path, gh=gh_with(prs))
    ws, sha = await _pushed_worktree(mgr, "t1", repo, "feat/owned")
    prs["feat/owned"] = [{"number": 8, "state": "MERGED", "headRefOid": sha}]
    await resolve(mgr, "t2", repo, branch="feat/owned")  # running again
    assert [r.reason for r in await mgr.reconcile()] == ["active owner task"]

    other_instance = manager(tmp_path, gh=gh_with(prs))
    assert [r.reason for r in await other_instance.reconcile()] == [
        "active owner task (another instance)"
    ]
    assert Path(ws.directory).exists()


@pytest.mark.asyncio
async def test_ttl_cleanup_when_gh_unavailable(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path, gh=gh_unavailable(), delegation_worktree_ttl_days=7)
    old, _ = await _pushed_worktree(mgr, "t1", repo, "feat/old")
    fresh, _ = await _pushed_worktree(mgr, "t2", repo, "feat/fresh")
    age_state(mgr, old, days=8)
    age_state(mgr, fresh, days=6)

    reports = {r.branch: (r.action, r.reason) for r in await mgr.reconcile()}

    assert reports == {
        "feat/old": ("removed", "TTL expired"),
        "feat/fresh": ("kept", "used within the TTL"),
    }
    assert not Path(old.directory).exists()
    assert git(repo, "rev-parse", "--verify", "refs/heads/feat/old")  # branch kept


@pytest.mark.asyncio
async def test_ttl_expired_fresh_worktree_without_commits_is_removed(
    tmp_path: Path, repo: Path
) -> None:
    mgr = manager(tmp_path)
    ws = await resolve(mgr, "t1", repo, branch="feat/empty")
    await mgr.release("t1")
    age_state(mgr, ws, days=30)
    assert [r.action for r in await mgr.reconcile()] == ["removed"]


@pytest.mark.asyncio
async def test_ttl_expired_but_unpushed_is_kept(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    ws = await resolve(mgr, "t1", repo, branch="feat/local-work")
    commit(Path(ws.directory), "work.txt")
    await mgr.release("t1")
    age_state(mgr, ws, days=30)
    assert [r.reason for r in await mgr.reconcile()] == ["unpushed commits"]


@pytest.mark.asyncio
async def test_user_created_worktrees_are_never_touched(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path, gh=gh_with({"user-branch": [{"state": "MERGED"}]}))
    user_wt = tmp_path / "private-claw-user-wt"
    git(repo, "worktree", "add", "-q", "-b", "user-branch", str(user_wt), "origin/master")
    # Even a worktree placed inside the managed root is ignored without a state file.
    inside = mgr.root / "somerepo" / "manual"
    inside.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-q", "-b", "manual", str(inside), "origin/master")
    managed = await resolve(mgr, "t1", repo, branch="feat/managed")
    await mgr.release("t1")
    age_state(mgr, managed, days=30)

    reports = await mgr.reconcile()

    assert [r.branch for r in reports] == ["feat/managed"]
    assert user_wt.is_dir() and inside.is_dir()
    listed = git(repo, "worktree", "list")
    assert str(user_wt.resolve()) in listed or str(user_wt) in listed


@pytest.mark.asyncio
async def test_state_of_vanished_worktree_is_forgotten(tmp_path: Path, repo: Path) -> None:
    import shutil

    mgr = manager(tmp_path)
    ws = await resolve(mgr, "t1", repo, branch="feat/gone")
    await mgr.release("t1")
    shutil.rmtree(ws.directory)
    assert [r.action for r in await mgr.reconcile()] == ["forgotten"]
    assert await mgr.reconcile() == []


@pytest.mark.asyncio
async def test_cleanup_runs_before_creating_a_new_worktree(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    old = await resolve(mgr, "t1", repo, branch="feat/stale")
    await mgr.release("t1")
    age_state(mgr, old, days=30)
    await resolve(mgr, "t2", repo, branch="feat/stale")  # reuse: no cleanup
    assert Path(old.directory).exists()
    await mgr.release("t2")
    age_state(mgr, old, days=30)
    await resolve(mgr, "t3", repo, branch="feat/new")  # creation: cleanup first
    assert not Path(old.directory).exists()


# --------------------------------------------------------------------------- coordinator


class _RecordingBackend(DelegationBackendAdapterInterface):
    def __init__(self, hold: float = 0.0) -> None:
        self.runs: list[DelegationRun] = []
        self.hold = hold
        self.active = 0
        self.max_active = 0

    @property
    def backend_id(self) -> str:
        return "claude_code"

    async def execute(self, request: DelegationRun) -> DelegationResult:
        self.runs.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.hold)
        finally:
            self.active -= 1
        return DelegationResult(ok=True, output_text="done")


async def _enqueue(
    coordinator: DelegationCoordinator, directory: Path | None, **workspace: str
) -> dict[str, Any]:
    request: dict[str, Any] = {"objective": "Do it", "tool_params": {}}
    if directory is not None:
        request["backend_params"] = {"directory": str(directory)}
    if workspace:
        request["workspace"] = workspace
    return await coordinator.enqueue_from_tool(
        session_id="tg:1", turn_id="turn", trace_id="trace", user_id="u", request=request
    )


async def _wait_status(
    coordinator: DelegationCoordinator, task_id: str, status: TaskStatus
) -> None:
    for _ in range(200):
        task = await coordinator.get_task(task_id)
        if task is not None and task.status == status:
            return
        await asyncio.sleep(0.05)
    task = await coordinator.get_task(task_id)
    raise AssertionError(f"{task_id}: {task.status if task else None}")


@pytest.mark.asyncio
async def test_coordinator_runs_repo_task_in_worktree_by_default(
    tmp_path: Path, repo: Path
) -> None:
    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    backend = _RecordingBackend()
    mgr = manager(tmp_path)
    coordinator = DelegationCoordinator(
        store=store, config=make_config(tmp_path / "data"), backends=[backend], workspaces=mgr
    )
    await coordinator.start()
    try:
        accepted = await _enqueue(coordinator, repo)  # old-style call: no workspace params
        assert accepted["accepted"] is True
        task_id = accepted["task_id"]
        task = await coordinator.get_task(task_id)
        assert task is not None
        workspace = task.metadata["workspace"]
        assert workspace["mode"] == "worktree"
        assert workspace["branch"] == f"subagent/{task_id}"
        assert task.metadata["backend_params"]["directory"] == workspace["directory"]
        assert workspace["directory"] != str(repo)
        await _wait_status(coordinator, task_id, TaskStatus.COMPLETED)
        run = backend.runs[0]
        assert run.backend_params["directory"] == workspace["directory"]
        assert run.objective.startswith("--- Environment (set by the coordinator) ---")
        assert workspace["directory"] in run.objective
        assert run.objective.endswith("--- Task ---\nDo it")
        assert task.metadata["objective"] == "Do it"  # stored objective unchanged
        assert mgr.owner_of(workspace["directory"]) is None  # released at the end
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_coordinator_rejects_second_task_on_owned_branch(tmp_path: Path, repo: Path) -> None:
    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    mgr = manager(tmp_path)
    coordinator = DelegationCoordinator(
        store=store,
        config=make_config(tmp_path / "data"),
        backends=[_RecordingBackend()],
        workspaces=mgr,
    )
    try:
        first = await _enqueue(coordinator, repo, branch="feat/x")
        assert first["accepted"] is True
        second = await _enqueue(coordinator, repo, branch="feat/x")
        assert second["accepted"] is False
        assert second["status"] == "rejected_workspace"
        assert second["retryable"] is True
        assert first["task_id"] in second["rejection_reason"]
        parallel = await _enqueue(coordinator, repo, branch="feat/y")
        assert parallel["accepted"] is True
        # Cancelling the pending owner frees the branch for the next task.
        await coordinator.cancel_task(first["task_id"])
        third = await _enqueue(coordinator, repo, branch="feat/x")
        assert third["accepted"] is True
    finally:
        await store.shutdown()


@pytest.mark.asyncio
async def test_coordinator_workspace_validation(tmp_path: Path, repo: Path) -> None:
    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=make_config(tmp_path / "data"),
        backends=[_RecordingBackend()],
        workspaces=manager(tmp_path),
    )
    try:
        no_dir = await _enqueue(coordinator, None, mode="worktree")
        assert no_dir["status"] == "rejected_invalid"
        assert "directory is required" in no_dir["rejection_reason"]
        bad_mode = await _enqueue(coordinator, repo, mode="scratch")
        assert bad_mode["status"] == "rejected_workspace"
        readonly = await _enqueue(coordinator, repo, mode="readonly")
        task = await coordinator.get_task(readonly["task_id"])
        assert task is not None
        assert task.metadata["backend_params"]["directory"] == str(repo)
        assert task.metadata["workspace"]["mode"] == "readonly"
    finally:
        await store.shutdown()


class _OtherBackend(_RecordingBackend):
    @property
    def backend_id(self) -> str:
        return "other_backend"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_id", ["claude_code", "claude_code_streaming"])
async def test_claude_code_delegation_without_directory_is_rejected(
    tmp_path: Path, backend_id: str
) -> None:
    class _Backend(_RecordingBackend):
        @property
        def backend_id(self) -> str:
            return backend_id

    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=make_config(tmp_path / "data"),
        backends=[_Backend(), _OtherBackend()],
        workspaces=manager(tmp_path),
    )
    try:
        for backend_params in (None, {}, {"directory": "  "}):
            request: dict[str, Any] = {"objective": "Do it", "backend": backend_id}
            if backend_params is not None:
                request["backend_params"] = backend_params
            result = await coordinator.enqueue_from_tool(
                session_id="tg:1", turn_id="turn", trace_id="trace", user_id="u", request=request
            )
            assert result["status"] == "rejected_invalid"
            assert "directory is required" in result["rejection_reason"]
        assert await store.tasks.list_by_status(TaskStatus.PENDING) == []
        # Other backends keep running without a directory (no workspace handling).
        other = await coordinator.enqueue_from_tool(
            session_id="tg:1",
            turn_id="turn",
            trace_id="trace",
            user_id="u",
            request={"objective": "Do it", "backend": "other_backend"},
        )
        assert other["accepted"] is True
        other_task = await coordinator.get_task(other["task_id"])
        assert other_task is not None and "workspace" not in other_task.metadata
    finally:
        await store.shutdown()


@pytest.mark.asyncio
async def test_coordinator_serializes_in_place_tasks(tmp_path: Path, repo: Path) -> None:
    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    backend = _RecordingBackend(hold=0.2)
    coordinator = DelegationCoordinator(
        store=store,
        config=make_config(tmp_path / "data", delegation_max_concurrent_tasks=4),
        backends=[backend],
        workspaces=manager(tmp_path),
    )
    await coordinator.start()
    try:
        ids = [(await _enqueue(coordinator, repo, mode="in_place"))["task_id"] for _ in range(3)]
        for task_id in ids:
            await _wait_status(coordinator, task_id, TaskStatus.COMPLETED)
        assert backend.max_active == 1
        assert all(run.backend_params["directory"] == str(repo) for run in backend.runs)
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_coordinator_runs_different_branches_in_parallel(tmp_path: Path, repo: Path) -> None:
    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    backend = _RecordingBackend(hold=0.3)
    coordinator = DelegationCoordinator(
        store=store,
        config=make_config(tmp_path / "data", delegation_max_concurrent_tasks=4),
        backends=[backend],
        workspaces=manager(tmp_path),
    )
    await coordinator.start()
    try:
        a = await _enqueue(coordinator, repo, branch="feat/a")
        b = await _enqueue(coordinator, repo, branch="feat/b")
        await _wait_status(coordinator, a["task_id"], TaskStatus.COMPLETED)
        await _wait_status(coordinator, b["task_id"], TaskStatus.COMPLETED)
        assert backend.max_active == 2
        assert len({run.backend_params["directory"] for run in backend.runs}) == 2
    finally:
        await coordinator.stop()
        await store.shutdown()


@pytest.mark.asyncio
async def test_coordinator_startup_reconciles_worktrees(tmp_path: Path, repo: Path) -> None:
    mgr = manager(tmp_path)
    stale = await resolve(mgr, "old", repo, branch="feat/stale")
    await mgr.release("old")
    age_state(mgr, stale, days=30)
    store = StoreFacade(data_root=tmp_path / "data")
    await store.initialize()
    coordinator = DelegationCoordinator(
        store=store,
        config=make_config(tmp_path / "data"),
        backends=[_RecordingBackend()],
        workspaces=mgr,
    )
    await coordinator.start()
    try:
        assert coordinator._reconcile_task is not None
        await asyncio.wait_for(coordinator._reconcile_task, timeout=10)
        assert not Path(stale.directory).exists()
    finally:
        await coordinator.stop()
        await store.shutdown()
