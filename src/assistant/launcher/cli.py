"""
Component ID: CMP_OPS_INSTANCE_LAUNCHER

``python -m assistant.launcher up|down|status [name]``: start, stop and inspect the
instances listed in ``<ASSISTANT_CONFIG_DIR>/instances.yaml``.
``check-single <target>``: used by ``make start / stop``; refuses when instances.yaml
exists, because the launcher then manages every instance.
Never prints token values, only token variable names.
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from assistant.launcher.instances import (
    BOT_TOKEN_ENV,
    DATA_DIR,
    INSTANCE_HOST,
    INSTANCES_FILENAME,
    InstanceSpec,
    LauncherError,
    build_child_env,
    load_env_lookup,
    load_instances,
    resolve_shared_config_dir,
)
from assistant.launcher.preflight import port_in_use, preflight
from assistant.launcher.process import (
    InstanceState,
    health_live,
    instance_state,
    spawn,
    stop_pid,
    uvicorn_log_path,
    wait_healthy,
)


@dataclass(frozen=True)
class LauncherContext:
    """Resolved inputs shared by all commands."""

    repo_root: Path
    config_dir: Path
    lookup: Mapping[str, str]
    base_env: Mapping[str, str]
    specs: list[InstanceSpec]
    out: Callable[[str], None] = print


def cmd_up(ctx: LauncherContext, selected: list[InstanceSpec]) -> int:
    """Start the selected instances that are not running; preflight first."""
    states = {s.name: instance_state(s) for s in selected}
    for spec in selected:
        state, pid = states[spec.name]
        if state is InstanceState.RUNNING:
            ctx.out(f"{spec.name}: already running (pid {pid}, port {spec.port})")
    to_start = [s for s in selected if states[s.name][0] is not InstanceState.RUNNING]
    if not to_start:
        return 0
    tokens = preflight(ctx.specs, to_start, ctx.lookup, ctx.config_dir, port_in_use)
    if ctx.lookup.get(BOT_TOKEN_ENV, "").strip():
        ctx.out(
            f"note: {BOT_TOKEN_ENV} is set (process env or .env) but no instance uses it: "
            "each one gets the token named by its token_env. Move it to a token_env "
            "variable and remove it (docs/DEVELOPMENT.md, 'Migrating')."
        )

    started = []
    for spec in to_start:
        if states[spec.name][0] is InstanceState.STALE:
            spec.pid_file.unlink(missing_ok=True)
            ctx.out(f"{spec.name}: removed stale PID file")
        env = build_child_env(
            spec,
            tokens[spec.name],
            ctx.base_env,
            ctx.config_dir,
            ctx.specs,
            shared_data_root=ctx.repo_root / DATA_DIR,
        )
        started.append((spec, spawn(spec, env, ctx.repo_root)))

    exit_code = 0
    for spec, proc in started:
        log = _display(uvicorn_log_path(spec), ctx.repo_root)
        result = wait_healthy(proc, spec.port)
        if result == "ok":
            ctx.out(f"{spec.name}: started, pid {proc.pid}, http://{INSTANCE_HOST}:{spec.port}")
        elif result == "exited":
            spec.pid_file.unlink(missing_ok=True)
            ctx.out(f"{spec.name}: FAILED, process exited during startup; see {log}")
            exit_code = 1
        else:
            ctx.out(f"{spec.name}: pid {proc.pid} started, /health/live not answering yet")
        ctx.out(f"{spec.name}: log {log}")
    return exit_code


def cmd_down(ctx: LauncherContext, selected: list[InstanceSpec]) -> int:
    """Stop the selected instances by PID file; never touches processes it did not start."""
    for spec in selected:
        state, pid = instance_state(spec)
        if state is InstanceState.STOPPED:
            ctx.out(f"{spec.name}: not running")
            continue
        if state is InstanceState.STALE or pid is None:
            spec.pid_file.unlink(missing_ok=True)
            ctx.out(f"{spec.name}: removed stale PID file (pid {pid} is not this instance)")
            continue
        ctx.out(f"{spec.name}: stopping pid {pid} ...")
        graceful = stop_pid(pid)
        spec.pid_file.unlink(missing_ok=True)
        ctx.out(f"{spec.name}: stopped" if graceful else f"{spec.name}: killed (SIGKILL)")
    return 0


def cmd_status(ctx: LauncherContext, selected: list[InstanceSpec]) -> int:
    """Print one line per instance: state, PID, health, token variable, paths."""
    header = ("NAME", "PORT", "STATE", "PID", "HEALTH", "TOKEN_ENV", "DATA_DIR", "LOG")
    rows = [header]
    for spec in selected:
        state, pid = instance_state(spec)
        health = "-"
        if state is InstanceState.RUNNING:
            health = "ok" if health_live(spec.port) else "down"
        rows.append(
            (
                spec.name,
                str(spec.port),
                state.value,
                str(pid) if pid is not None else "-",
                health,
                spec.token_env or "-",
                _display(spec.data_dir, ctx.repo_root),
                _display(uvicorn_log_path(spec), ctx.repo_root),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for row in rows:
        ctx.out(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )
    return 0


_CHECK_SINGLE = "check-single"
_COMMANDS: dict[str, Callable[[LauncherContext, list[InstanceSpec]], int]] = {
    "up": cmd_up,
    "down": cmd_down,
    "status": cmd_status,
}


def build_context(environ: Mapping[str, str], repo_root: Path) -> LauncherContext:
    """Resolve the shared config dir and load instances.yaml."""
    lookup = load_env_lookup(repo_root, environ)
    config_dir = resolve_shared_config_dir(lookup, repo_root)
    specs = load_instances(config_dir, repo_root)
    return LauncherContext(repo_root, config_dir, lookup, environ, specs)


def check_single(environ: Mapping[str, str], repo_root: Path, target: str | None) -> int:
    """``make start / stop`` guard: 0 without instances.yaml, else 2 with a hint."""
    lookup = load_env_lookup(repo_root, environ)
    path = resolve_shared_config_dir(lookup, repo_root) / INSTANCES_FILENAME
    if not path.is_file():
        return 0
    print(
        f"make {target or 'start'}: refused: {_display(path, repo_root)} exists, so the "
        "launcher manages every instance.\nUse make up / make down / make status "
        "[NAME=<instance>]. To run a single process with make start again, remove or "
        "rename instances.yaml.",
        file=sys.stderr,
    )
    return 2


def select(specs: list[InstanceSpec], name: str | None) -> list[InstanceSpec]:
    """All instances, or the one named ``name``."""
    if not name:
        return specs
    chosen = [s for s in specs if s.name == name]
    if not chosen:
        known = ", ".join(s.name for s in specs) or "none"
        raise LauncherError([f"unknown instance '{name}' (configured: {known})"])
    return chosen


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m assistant.launcher")
    parser.add_argument("command", choices=[*sorted(_COMMANDS), _CHECK_SINGLE])
    parser.add_argument("name", nargs="?", default=None)
    args = parser.parse_args(argv)
    root = (repo_root or Path.cwd()).resolve()
    env = os.environ if environ is None else environ
    if args.command == _CHECK_SINGLE:
        return check_single(env, root, args.name)
    try:
        ctx = build_context(env, root)
        selected = select(ctx.specs, args.name)
        if not selected:
            ctx.out(f"No instances configured in {ctx.config_dir / 'instances.yaml'}")
            return 0
        return _COMMANDS[args.command](ctx, selected)
    except LauncherError as exc:
        action = "refused, nothing started" if args.command == "up" else "failed"
        print(f"launcher {args.command}: {action}:\n{exc}", file=sys.stderr)
        return 2


def _display(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)
