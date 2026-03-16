"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

macOS Notes and Reminders tools via AppleScript (osascript).
Read/write operations with platform guard, timeout, and normalized responses.
"""

import subprocess
import sys
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)

_MAX_TIMEOUT_SECONDS = 60
_RS = "\x1e"  # record separator
_US = "\x1f"  # unit separator


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _run_applescript(
    script: str,
    args: list[str] | None = None,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    cmd = ["osascript", "-e", script]
    if args:
        cmd.extend(["--", *args])
    bounded = max(1, min(timeout_seconds, _MAX_TIMEOUT_SECONDS))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=bounded,
        )
        return {
            "status": "ok" if result.returncode == 0 else "failed",
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
            "reason": result.stderr.strip() if result.returncode != 0 and result.stderr else None,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "stdout": "",
            "stderr": "",
            "reason": f"osascript timed out after {bounded}s",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "stdout": "",
            "stderr": "",
            "reason": str(exc),
        }


def _normalize_result(raw: dict[str, Any], tool_name: str) -> dict[str, Any]:
    status = raw.get("status", "failed")
    reason = raw.get("reason")
    stdout = raw.get("stdout", "")
    stderr = raw.get("stderr", "")
    out: dict[str, Any] = {"status": status}
    if reason:
        out["reason"] = reason
    if status == "ok":
        out["data"] = stdout
    else:
        out["stdout"] = stdout
        out["stderr"] = stderr
    return out


def macos_notes_read(
    ctx: RunContext[TurnDeps],
    folder_name: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """List recent Notes with name and body. Optional folder filter."""
    if not _is_macos():
        logger.info(
            "provider.tool_call.macos_notes_read",
            status="rejected_platform",
            platform=sys.platform,
        )
        return {"status": "rejected_platform", "reason": "macOS only", "data": ""}

    limit = max(1, min(limit, 50))
    script = f'''
on run argv
  set theLimit to item 1 of argv as integer
  set theFolder to item 2 of argv
  tell application "Notes"
    set noteList to {{}}
    if theFolder is not "" then
      try
        set targetFolder to folder theFolder
        set noteList to notes of targetFolder
      on error
        return ""
      end try
    else
      set noteList to every note
    end if
    set out to ""
    set cnt to 0
    repeat with n in noteList
      if cnt >= theLimit then exit repeat
      if (length of out) > 10000 then exit repeat
      try
        set nname to name of n
        set nbody to ""
        try
          set nbody to body of n
        end try
        set out to out & nname & "{_US}" & nbody & "{_RS}"
        set cnt to cnt + 1
      end try
    end repeat
    return out
  end tell
end run
'''
    raw = _run_applescript(script, [str(limit), folder_name or ""])
    if raw["status"] != "ok":
        out = _normalize_result(raw, "macos_notes_read")
        logger.warning(
            "provider.tool_call.macos_notes_read",
            status=raw["status"],
            reason=raw.get("reason"),
        )
        return out

    records = []
    for block in (raw.get("stdout", "") or "").split(_RS):
        if not block.strip():
            continue
        parts = block.split(_US, 1)
        name = parts[0].strip() if parts else ""
        body = parts[1].strip() if len(parts) > 1 else ""
        records.append({"name": name, "body": body})
        if len(records) >= limit:
            break

    logger.info(
        "provider.tool_call.macos_notes_read",
        status="ok",
        count=len(records),
    )
    return {"status": "ok", "data": records, "count": len(records)}


def macos_notes_write(
    ctx: RunContext[TurnDeps],
    title: str,
    body: str = "",
) -> dict[str, Any]:
    """Create a new Note with given title and body."""
    if not _is_macos():
        logger.info(
            "provider.tool_call.macos_notes_write",
            status="rejected_platform",
            platform=sys.platform,
        )
        return {"status": "rejected_platform", "reason": "macOS only", "data": ""}

    title = (title or "").strip()
    if not title:
        logger.info(
            "provider.tool_call.macos_notes_write",
            status="rejected_invalid",
            reason="title is empty",
        )
        return {"status": "rejected_invalid", "reason": "title is required", "data": ""}

    script = """
on run argv
  set theTitle to item 1 of argv
  set theBody to item 2 of argv
  tell application "Notes"
    make new note with properties {{name:theTitle, body:theBody}}
    return "created"
  end tell
end run
"""
    raw = _run_applescript(script, [title, body or ""])
    out = _normalize_result(raw, "macos_notes_write")
    if raw["status"] == "ok":
        out["data"] = {"created": True, "title": title}
        logger.info("provider.tool_call.macos_notes_write", status="ok", title=title)
    else:
        logger.warning(
            "provider.tool_call.macos_notes_write",
            status=raw["status"],
            reason=raw.get("reason"),
        )
    return out


def macos_reminders_read(
    ctx: RunContext[TurnDeps],
    list_name: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List reminders, optionally from a specific list."""
    if not _is_macos():
        logger.info(
            "provider.tool_call.macos_reminders_read",
            status="rejected_platform",
            platform=sys.platform,
        )
        return {"status": "rejected_platform", "reason": "macOS only", "data": ""}

    limit = max(1, min(limit, 100))
    script = f'''
on run argv
  set theLimit to item 1 of argv as integer
  set theListName to item 2 of argv
  tell application "Reminders"
    set out to ""
    set cnt to 0
    if theListName is not "" then
      set targetList to list theListName
      set listList to {{targetList}}
    else
      set listList to every list
    end if
    repeat with lst in listList
      if cnt >= theLimit then exit repeat
      repeat with r in (reminders of lst)
        if cnt >= theLimit then exit repeat
        if completed of r is false then
          try
            set rname to name of r
            set rbody to ""
            try
              set rbody to body of r
            end try
            set rdue to ""
            try
              set rdue to (due date of r) as text
            end try
            set out to out & rname & "{_US}" & rbody & "{_US}" & rdue & "{_RS}"
            set cnt to cnt + 1
          end try
        end if
      end repeat
    end repeat
    return out
  end tell
end run
'''
    raw = _run_applescript(script, [str(limit), list_name or ""])
    if raw["status"] != "ok":
        out = _normalize_result(raw, "macos_reminders_read")
        logger.warning(
            "provider.tool_call.macos_reminders_read",
            status=raw["status"],
            reason=raw.get("reason"),
        )
        return out

    records = []
    for block in (raw.get("stdout", "") or "").split(_RS):
        if not block.strip():
            continue
        parts = block.split(_US, 2)
        name = parts[0].strip() if parts else ""
        body = parts[1].strip() if len(parts) > 1 else ""
        due = parts[2].strip() if len(parts) > 2 else ""
        records.append({"name": name, "body": body, "due_date": due or None})

    logger.info(
        "provider.tool_call.macos_reminders_read",
        status="ok",
        count=len(records),
    )
    return {"status": "ok", "data": records, "count": len(records)}


def macos_reminders_write(
    ctx: RunContext[TurnDeps],
    title: str,
    body: str = "",
    list_name: str | None = None,
    due_year: int | None = None,
    due_month: int | None = None,
    due_day: int | None = None,
    due_hour: int | None = None,
    due_minute: int | None = None,
) -> dict[str, Any]:
    """Create a new Reminder with given title, optional body, list, and due/remind date.

    For a reminder with a notification trigger, pass due_year, due_month, due_day, due_hour,
    due_minute (all required together). The agent must compute the concrete date/time and pass
    these integers. E.g. for "March 17, 2026 11:00 AM": due_year=2026, due_month=3, due_day=17,
    due_hour=11, due_minute=0. Both due date and remind-me (notification trigger) are set.
    """
    if not _is_macos():
        logger.info(
            "provider.tool_call.macos_reminders_write",
            status="rejected_platform",
            platform=sys.platform,
        )
        return {"status": "rejected_platform", "reason": "macOS only", "data": ""}

    title = (title or "").strip()
    if not title:
        logger.info(
            "provider.tool_call.macos_reminders_write",
            status="rejected_invalid",
            reason="title is empty",
        )
        return {"status": "rejected_invalid", "reason": "title is required", "data": ""}

    has_date = all(x is not None for x in (due_year, due_month, due_day, due_hour, due_minute))
    if has_date:
        args = [
            title,
            body or "",
            list_name or "",
            str(due_year),
            str(due_month),
            str(due_day),
            str(due_hour),
            str(due_minute),
        ]
    else:
        args = [title, body or "", list_name or "", "", "", "", "", ""]

    script = """
on run argv
  set theTitle to item 1 of argv
  set theBody to item 2 of argv
  set theListName to item 3 of argv
  set theYear to item 4 of argv
  set theDate to missing value
  if theYear is not "" then
    set theMonth to item 5 of argv as integer
    set theDay to item 6 of argv as integer
    set theHour to item 7 of argv as integer
    set theMin to item 8 of argv as integer
    set baseDate to current date
    tell baseDate
      set its day to 1
      set its month to theMonth
      set its year to theYear as integer
      set its day to theDay
      set its time to (theHour * hours + theMin * minutes)
    end tell
    set theDate to baseDate
  end if
  tell application "Reminders"
    if theListName is not "" then
      set targetList to list theListName
    else
      set targetList to default list
    end if
    tell targetList
      if theDate is not missing value then
        make new reminder at end with properties {{name:theTitle, body:theBody, due date:theDate, remind me date:theDate}}
      else
        make new reminder at end with properties {{name:theTitle, body:theBody}}
      end if
    end tell
    return "created"
  end tell
end run
"""
    raw = _run_applescript(script, args, timeout_seconds=45)
    out = _normalize_result(raw, "macos_reminders_write")
    if raw["status"] == "ok":
        out["data"] = {"created": True, "title": title}
        log_extra: dict[str, Any] = {"status": "ok", "title": title}
        if has_date:
            log_extra["due"] = (
                f"{due_year}-{due_month:02d}-{due_day:02d} {due_hour:02d}:{due_minute:02d}"
            )
        logger.info("provider.tool_call.macos_reminders_write", **log_extra)
    else:
        logger.warning(
            "provider.tool_call.macos_reminders_write",
            status=raw["status"],
            reason=raw.get("reason"),
        )
    return out
