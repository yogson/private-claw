"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Background refresh of the claude-obsidian retrieval index (github.com/AgriciDaniel/claude-obsidian).

obsidian_wiki_search reads a prebuilt BM25 index (.vault-meta/bm25/index.json) that
nothing else keeps current: the vault is also written by Claude Code sub-agents running
the plugin and by the user in Obsidian, neither of which reindexes. So the index is
refreshed here, off the request path:

- obsidian_wiki_write/edit request a refresh after every successful markdown write;
- obsidian_wiki_search requests one whenever the index is missing or older than any
  wiki page (cheap mtime scan), and serves the current index meanwhile.

A refresh is an incremental `contextual-prefix.py --all --no-llm` (unchanged chunks
are skipped, deleted pages pruned) followed by a full `bm25-index.py build`. Runs are
single-flight per vault: a request arriving mid-run marks the vault dirty and triggers
exactly one follow-up run. Both scripts take the vault mutation lock with timeout 0
and fail closed when it is busy, so failed runs are retried with a short backoff.
"""

import os
import subprocess
import threading
import time

import structlog

logger = structlog.get_logger(__name__)

_STEP_TIMEOUT_SECONDS = 120
_RETRY_DELAYS_SECONDS = (2.0, 5.0, 15.0)
_INDEX_RELATIVE_PATH = os.path.join(".vault-meta", "bm25", "index.json")

_state_lock = threading.Lock()
_running: set[tuple[str, str]] = set()
_dirty: set[tuple[str, str]] = set()


def index_is_stale(vault: str) -> bool:
    """True if the BM25 index is missing or older than any page/dir under wiki/.

    Directory mtimes are included so deleted or renamed pages also count.
    """
    try:
        index_mtime = os.path.getmtime(os.path.join(vault, _INDEX_RELATIVE_PATH))
    except OSError:
        return True
    wiki_root = os.path.join(vault, "wiki")
    for dirpath, _dirnames, filenames in os.walk(wiki_root):
        try:
            if os.path.getmtime(dirpath) > index_mtime:
                return True
        except OSError:
            continue
        for name in filenames:
            if not name.endswith(".md"):
                continue
            try:
                if os.path.getmtime(os.path.join(dirpath, name)) > index_mtime:
                    return True
            except OSError:
                continue
    return False


def schedule_reindex(product_root: str, vault: str, *, reason: str) -> bool:
    """Request a background index refresh. Returns True if a new worker was started.

    Never blocks and never raises; if a refresh is already running for this vault,
    the request is coalesced into one follow-up run.
    """
    key = (product_root, vault)
    with _state_lock:
        if key in _running:
            _dirty.add(key)
            logger.info("obsidian_wiki_index.reindex_coalesced", reason=reason)
            return False
        _running.add(key)
    try:
        threading.Thread(
            target=_worker,
            args=(product_root, vault),
            name="obsidian-wiki-reindex",
            daemon=True,
        ).start()
    except RuntimeError as exc:
        with _state_lock:
            _running.discard(key)
        logger.warning("obsidian_wiki_index.reindex_not_started", reason=str(exc))
        return False
    logger.info("obsidian_wiki_index.reindex_scheduled", reason=reason)
    return True


def _worker(product_root: str, vault: str) -> None:
    key = (product_root, vault)
    released = False
    try:
        while True:
            with _state_lock:
                _dirty.discard(key)
            _reindex_with_retries(product_root, vault)
            with _state_lock:
                if key not in _dirty:
                    _running.discard(key)
                    released = True
                    return
    except Exception:
        logger.exception("obsidian_wiki_index.reindex_crashed")
    finally:
        # Unexpected failure: free the slot (dropping any queued follow-up) so the
        # next schedule_reindex can start a fresh worker instead of coalescing forever.
        if not released:
            with _state_lock:
                _running.discard(key)
                _dirty.discard(key)


def _reindex_with_retries(product_root: str, vault: str) -> None:
    error = _reindex_once(product_root, vault)
    for delay in _RETRY_DELAYS_SECONDS:
        if error is None:
            return
        logger.info("obsidian_wiki_index.reindex_retry", delay=delay, reason=error)
        time.sleep(delay)
        error = _reindex_once(product_root, vault)
    if error is not None:
        logger.warning("obsidian_wiki_index.reindex_failed", reason=error)


def _reindex_once(product_root: str, vault: str) -> str | None:
    """Run one refresh. Returns an error string, or None on success."""
    scripts = os.path.join(product_root, "scripts")
    prefix_script = os.path.join(scripts, "contextual-prefix.py")
    bm25_script = os.path.join(scripts, "bm25-index.py")
    steps = [
        ["python3", prefix_script, "--vault", vault, "--all", "--no-llm"],
        ["python3", bm25_script, "--vault", vault, "build"],
    ]
    started = time.monotonic()
    for cmd in steps:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_STEP_TIMEOUT_SECONDS
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        if proc.returncode != 0:
            script = os.path.basename(cmd[1])
            return proc.stderr.strip()[-500:] or f"{script} exited {proc.returncode}"
    logger.info("obsidian_wiki_index.reindex_done", duration_s=round(time.monotonic() - started, 2))
    return None
