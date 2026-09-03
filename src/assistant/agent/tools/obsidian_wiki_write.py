"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Direct write tool over a claude-obsidian vault (github.com/AgriciDaniel/claude-obsidian).

Builds a claude-obsidian.transaction.v1 bundle and applies it via the product's
CLI transaction engine (inspect -> apply), synchronously, in-process — no nested
Claude Code sub-agent involved. The engine itself enforces the vault's mutation
lock, SHA-256 write preconditions, and recoverability; this tool only computes
those preconditions and shells out to the two CLI calls. See
skills/wiki/references/operation-transactions.md in the product checkout for the
full bundle contract.
"""

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import PurePosixPath
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)

_SUBPROCESS_TIMEOUT_SECONDS = 30
_INDEX_REFRESH_TIMEOUT_SECONDS = 20
_EXIT_CONFLICT = 75
_MAX_WRITES_PER_CALL = 20
_ALLOWED_MODES = {"create", "replace"}


def _reject_reason(path: str) -> str | None:
    """Return a reason string if a vault-relative write path is unsafe, else None."""
    if not path or not path.strip():
        return "path must not be empty"
    rel = PurePosixPath(path)
    if rel.is_absolute():
        return f"path must be vault-relative, not absolute: {path}"
    if ".." in rel.parts:
        return f"path must not contain '..': {path}"
    return None


def obsidian_wiki_write(
    ctx: RunContext[TurnDeps],
    operation_type: str,
    writes: list[dict[str, str]],
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Write one or more notes into the Obsidian vault as a single reviewed transaction.

    Complements obsidian_wiki_search: use this to save an answer/insight the user
    explicitly asked to preserve, or to update existing vault notes. Only write what
    was explicitly requested — never save a whole conversation. A proper `save`
    normally couples the new/updated note with wiki/index.md (or the active
    methodology index) and wiki/log.md (one new top-of-file entry); update
    wiki/hot.md too when it materially changes. Keep each write self-contained
    Markdown with honest YAML frontmatter (type/title/status/created/updated/tags).

    Args:
        operation_type: One of the vault's operation types, most commonly "save" for
            a reviewed answer/insight, "markdown" for a direct note edit, or
            "generic" for anything else confined to wiki/. Determines which paths the
            transaction is allowed to touch (e.g. "save" is confined to wiki/).
        writes: List of {"path": <vault-relative path>, "mode": "create"|"replace",
            "content": <full file content>}. Use "create" for a brand-new file and
            "replace" only when you have just read the current file and are updating
            it. Paths must stay under wiki/ and must not use ".." or be absolute.
        operation_id: Optional stable ID for this operation; auto-generated if omitted.
    """
    vault = os.getenv("CLAUDE_OBSIDIAN_VAULT")
    product_root = os.getenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT")
    if not vault or not product_root:
        return {"status": "unavailable", "reason": "obsidian vault not configured"}

    logger.info(
        "provider.tool_call.obsidian_wiki_write",
        phase="entry",
        operation_type=operation_type,
        write_count=len(writes) if isinstance(writes, list) else 0,
    )

    if not isinstance(writes, list) or not writes:
        return {"status": "rejected_invalid", "reason": "writes must be a non-empty list"}
    if len(writes) > _MAX_WRITES_PER_CALL:
        return {
            "status": "rejected_invalid",
            "reason": f"too many writes in one call (max {_MAX_WRITES_PER_CALL})",
        }

    expected_hashes: dict[str, str | None] = {}
    bundle_writes: list[dict[str, Any]] = []
    for entry in writes:
        path = str(entry.get("path", ""))
        mode = str(entry.get("mode", ""))
        content = entry.get("content")

        reason = _reject_reason(path)
        if reason is not None:
            return {"status": "rejected_invalid", "reason": reason}
        if mode not in _ALLOWED_MODES:
            return {
                "status": "rejected_invalid",
                "reason": f"mode must be 'create' or 'replace', got {mode!r} for {path}",
            }
        if not isinstance(content, str) or not content:
            return {"status": "rejected_invalid", "reason": f"content must be non-empty for {path}"}

        target = os.path.join(vault, path)
        exists = os.path.isfile(target)
        if mode == "replace" and not exists:
            return {
                "status": "rejected_invalid",
                "reason": f"cannot replace missing file (use mode=create instead): {path}",
            }
        if mode == "create" and exists:
            return {
                "status": "rejected_invalid",
                "reason": f"cannot create — file already exists (use mode=replace instead): {path}",
            }

        if mode == "replace":
            with open(target, encoding="utf-8") as f:
                current_hash = hashlib.sha256(f.read().encode("utf-8")).hexdigest()
            expected_hashes[path] = current_hash
        else:
            expected_hashes[path] = None

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        bundle_writes.append(
            {"path": path, "mode": mode, "content": content, "sha256": content_hash}
        )

    resolved_operation_id = operation_id or f"{operation_type}-{uuid.uuid4().hex[:12]}"
    bundle = {
        "schema": "claude-obsidian.transaction.v1",
        "operation_id": resolved_operation_id,
        "operation_type": operation_type,
        "expected_hashes": expected_hashes,
        "writes": bundle_writes,
        "address_requests": [],
        "source_manifest_updates": {},
    }

    core_script = os.path.join(product_root, "scripts", "claude-obsidian.py")
    bundle_fd, bundle_path = tempfile.mkstemp(suffix=".json", prefix="obsidian-bundle-")
    try:
        with os.fdopen(bundle_fd, "w", encoding="utf-8") as f:
            json.dump(bundle, f)

        inspect_result = _run_cli(
            ["python3", core_script, "transaction", "inspect", bundle_path, "--vault", vault]
        )
        if inspect_result["status"] != "ok":
            return inspect_result
        approval_sha256 = inspect_result["payload"].get("approval_sha256")
        if not isinstance(approval_sha256, str) or not approval_sha256:
            return {"status": "failed", "reason": "inspect result missing approval_sha256"}

        apply_result = _run_cli(
            [
                "python3",
                core_script,
                "transaction",
                "apply",
                bundle_path,
                "--vault",
                vault,
                "--approved-plan-sha256",
                approval_sha256,
            ]
        )
        if apply_result["status"] == "conflict":
            logger.warning("provider.tool_result.obsidian_wiki_write", status="conflict")
            return apply_result
        if apply_result["status"] != "ok":
            logger.warning(
                "provider.tool_result.obsidian_wiki_write",
                status=apply_result["status"],
                reason=apply_result.get("reason"),
            )
            return apply_result

        changed_paths = apply_result["payload"].get("changed_paths", [])
        result: dict[str, Any] = {
            "status": "ok",
            "operation_id": apply_result["payload"].get("operation_id", resolved_operation_id),
            "changed_paths": changed_paths,
        }
        result.update(_refresh_retrieval_index(product_root, vault, changed_paths))
        logger.info(
            "provider.tool_result.obsidian_wiki_write",
            status="ok",
            changed_paths=result["changed_paths"],
            index_refreshed=result.get("index_refreshed"),
        )
        return result
    finally:
        with contextlib.suppress(OSError):
            os.unlink(bundle_path)


def _refresh_retrieval_index(
    product_root: str, vault: str, changed_paths: list[str]
) -> dict[str, Any]:
    """Best-effort: re-provision BM25 retrieval for changed wiki pages after a write.

    obsidian_wiki_search reads a prebuilt index that this tool would otherwise leave
    stale (contextual-prefix.py / bm25-index.py are separate steps). Failure here
    never turns a successful content write into a failure — it only annotates the
    result so the model can tell the user retrieval may lag.
    """
    md_paths = [
        p
        for p in changed_paths
        if isinstance(p, str) and p.startswith("wiki/") and p.endswith(".md")
    ]
    if not md_paths:
        return {"index_refreshed": False}

    prefix_script = os.path.join(product_root, "scripts", "contextual-prefix.py")
    bm25_script = os.path.join(product_root, "scripts", "bm25-index.py")
    try:
        for path in md_paths:
            proc = subprocess.run(
                ["python3", prefix_script, "--vault", vault, path, "--no-llm"],
                capture_output=True,
                text=True,
                timeout=_INDEX_REFRESH_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0:
                reason = proc.stderr.strip() or f"contextual-prefix.py exited {proc.returncode}"
                return {"index_refreshed": False, "index_refresh_error": reason}

        proc = subprocess.run(
            ["python3", bm25_script, "--vault", vault, "build"],
            capture_output=True,
            text=True,
            timeout=_INDEX_REFRESH_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            reason = proc.stderr.strip() or f"bm25-index.py build exited {proc.returncode}"
            return {"index_refreshed": False, "index_refresh_error": reason}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"index_refreshed": False, "index_refresh_error": str(exc)}

    return {"index_refreshed": True}


def _run_cli(cmd: list[str]) -> dict[str, Any]:
    """Run one claude-obsidian.py CLI call and normalize its result."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "reason": str(exc)}

    if proc.returncode == _EXIT_CONFLICT:
        return {
            "status": "conflict",
            "reason": "vault changed or is locked by another operation — re-read and retry",
        }
    if proc.returncode != 0:
        reason = proc.stderr.strip() or proc.stdout.strip() or f"exited with code {proc.returncode}"
        return {"status": "failed", "reason": reason}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"status": "failed", "reason": f"invalid CLI output: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "failed", "reason": "unexpected CLI output shape"}
    return {"status": "ok", "payload": payload}
