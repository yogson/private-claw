"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Persisted intent audit for cross-process idempotency.
"""

import json
import os
from pathlib import Path

AUDIT_FILENAME = "memory_intent_audit.jsonl"


def _audit_path(data_root: Path) -> Path:
    return data_root / "runtime" / AUDIT_FILENAME


def load_seen_intent_ids(data_root: Path) -> set[str]:
    """Load intent_ids from persisted audit log."""
    path = _audit_path(data_root)
    seen: set[str] = set()
    if not path.exists():
        return seen
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec.get("intent_id"), str):
                    seen.add(rec["intent_id"])
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return seen


def append_audit(data_root: Path, intent_id: str, status: str, memory_id: str | None) -> None:
    """Append one audit record to the log."""
    path = _audit_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"intent_id": intent_id, "status": status, "memory_id": memory_id}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
