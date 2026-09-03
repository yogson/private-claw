"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Read-only retrieval tool over a claude-obsidian vault (github.com/AgriciDaniel/claude-obsidian).

Shells out to the product's deterministic retrieve.py pipeline (BM25 over
contextualized chunks, optional cosine rerank) rather than delegating to a Claude
Code sub-agent, so it stays synchronous and cheap enough to call like memory_search.
Requires CLAUDE_OBSIDIAN_VAULT and CLAUDE_OBSIDIAN_PRODUCT_ROOT env vars; the tool
is only useful once the vault's retrieval index has been provisioned (see
scripts/contextual-prefix.py / bm25-index.py in the product checkout).
"""

import json
import os
import subprocess
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)

_SUBPROCESS_TIMEOUT_SECONDS = 20
_EXIT_NOT_PROVISIONED = 10


def obsidian_wiki_search(
    ctx: RunContext[TurnDeps],
    query: str,
    top: int = 5,
) -> dict[str, Any]:
    """Search the Obsidian knowledge vault for passages relevant to a query.

    Complements memory_search: this reads durable, linked, source-cited notes
    (people, projects, decisions, reference facts) from the vault instead of the
    structured facts/profile memory store. Read-only; never modifies the vault.
    To add or update vault content, delegate to a sub-agent running the
    claude-obsidian plugin (see the obsidian_wiki capability prompt).
    """
    vault = os.getenv("CLAUDE_OBSIDIAN_VAULT")
    product_root = os.getenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT")
    if not vault or not product_root:
        return {"status": "unavailable", "reason": "obsidian vault not configured", "matches": []}

    bounded_top = max(1, min(top, 20))
    retrieve_script = os.path.join(product_root, "scripts", "retrieve.py")
    cmd = [
        "python3",
        retrieve_script,
        "--vault",
        vault,
        query,
        "--top",
        str(bounded_top),
        "--no-rerank",
    ]
    logger.info(
        "provider.tool_call.obsidian_wiki_search", phase="entry", query=query, top=bounded_top
    )
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result: dict[str, Any] = {"status": "failed", "reason": str(exc), "matches": []}
        logger.warning(
            "provider.tool_result.obsidian_wiki_search", status="failed", reason=str(exc)
        )
        return result

    if proc.returncode == _EXIT_NOT_PROVISIONED:
        result = {
            "status": "not_provisioned",
            "reason": "vault retrieval index is not built yet",
            "matches": [],
        }
        logger.info("provider.tool_result.obsidian_wiki_search", status=result["status"])
        return result
    if proc.returncode != 0:
        reason = proc.stderr.strip() or f"retrieve.py exited with code {proc.returncode}"
        result = {"status": "failed", "reason": reason, "matches": []}
        logger.warning("provider.tool_result.obsidian_wiki_search", status="failed", reason=reason)
        return result

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        result = {"status": "failed", "reason": f"invalid retrieve.py output: {exc}", "matches": []}
        logger.warning(
            "provider.tool_result.obsidian_wiki_search", status="failed", reason=str(exc)
        )
        return result

    candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
    matches: list[dict[str, Any]] = [
        {
            "page_path": c.get("page_path"),
            "snippet": c.get("snippet"),
            "score": c.get("rerank_score", c.get("bm25_score")),
        }
        for c in candidates
        if isinstance(c, dict)
    ]
    result = {"status": "ok", "matches": matches}
    logger.info(
        "provider.tool_result.obsidian_wiki_search", status="ok", match_count=len(matches)
    )
    return result
