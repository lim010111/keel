#!/usr/bin/env python3
"""Collect Claude Code session activity for a given date (default: today, local tz).

Walks ~/.claude/projects/*/<sessionId>.jsonl, finds sessions with any message
timestamped on the target *local* date, and emits a JSON report grouped by
project (cwd). Each session includes user prompts, files touched, bash
descriptions, skills/agents invoked, and a UTC time range.

Output: JSON to stdout. Intended to be consumed by the daily-dev-log skill,
which then composes a Korean markdown summary.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Sessions started before this local hour are attributed to the previous
# calendar day (late-night work continuation). A "day" here means
# [target 05:00, target+1 05:00) in local time.
LATE_NIGHT_CUTOFF_HOUR = 5

COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")
COMMAND_ARGS_RE = re.compile(r"<command-args>([^<]*)</command-args>", re.DOTALL)

META_MARKERS = (
    "<system-reminder>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<command-message>",
    "[Request interrupted",
)

MAX_PROMPT_CHARS = 500
MAX_PROMPTS_PER_SESSION = 30
MAX_BASH_PER_SESSION = 20
MAX_FILES_PER_SESSION = 40


def parse_ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def local_date(ts):
    if ts is None:
        return None
    return ts.astimezone().date()


def logical_date(ts):
    if ts is None:
        return None
    local = ts.astimezone()
    if local.hour < LATE_NIGHT_CUTOFF_HOUR:
        return (local - timedelta(days=1)).date()
    return local.date()


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n".join(parts)
    return ""


def classify_user_message(msg):
    raw = msg.get("message", {})
    content = raw.get("content", "")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return "tool_result", ""
    text = extract_text(content)
    if msg.get("isMeta"):
        return "meta", text
    if "<command-name>" in text and "<command-args>" in text:
        m = COMMAND_NAME_RE.search(text)
        a = COMMAND_ARGS_RE.search(text)
        cmd = m.group(1) if m else ""
        args = (a.group(1) if a else "").strip()
        return "command", f"/{cmd.strip().lstrip('/')} {args}".strip()
    if any(marker in text for marker in META_MARKERS):
        return "meta", text
    return "prompt", text.strip()


def extract_tool_uses(msg):
    out = []
    content = msg.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input", {}) or {}
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            summary = inp.get("file_path", "")
        elif name == "Read":
            summary = inp.get("file_path", "")
        elif name == "Bash":
            summary = inp.get("description") or (inp.get("command", "")[:80])
        elif name == "Agent":
            summary = inp.get("description", "") or inp.get("subagent_type", "")
        elif name == "Skill":
            summary = inp.get("skill", "")
        else:
            summary = ""
        out.append((name, summary))
    return out


def process_session(path: Path, target_date) -> dict | None:
    cwd = None
    git_branch = None
    first_ts = None
    last_ts = None
    prompts: list[dict] = []
    commands: list[dict] = []
    files_edited: set[str] = set()
    files_read: set[str] = set()
    bash_descs: list[str] = []
    agents: list[str] = []
    skills: list[str] = []
    user_msg_count = 0
    asst_msg_count = 0
    on_target = False

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = parse_ts(obj.get("timestamp"))
                if ts:
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                    if logical_date(ts) == target_date:
                        on_target = True

                if obj.get("cwd") and not cwd:
                    cwd = obj["cwd"]
                if obj.get("gitBranch") and not git_branch:
                    git_branch = obj["gitBranch"]

                t = obj.get("type")
                if t == "user":
                    kind, text = classify_user_message(obj)
                    if kind == "prompt" and text:
                        user_msg_count += 1
                        if len(prompts) < MAX_PROMPTS_PER_SESSION:
                            prompts.append(
                                {
                                    "ts": obj.get("timestamp"),
                                    "text": text[:MAX_PROMPT_CHARS],
                                }
                            )
                    elif kind == "command":
                        commands.append(
                            {"ts": obj.get("timestamp"), "text": text[:200]}
                        )
                elif t == "assistant":
                    asst_msg_count += 1
                    for name, summary in extract_tool_uses(obj):
                        if not summary:
                            continue
                        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                            files_edited.add(summary)
                        elif name == "Read":
                            files_read.add(summary)
                        elif name == "Bash":
                            if len(bash_descs) < MAX_BASH_PER_SESSION:
                                bash_descs.append(summary)
                        elif name == "Agent":
                            agents.append(summary)
                        elif name == "Skill":
                            skills.append(summary)
    except OSError:
        return None

    if not on_target:
        return None

    files_edited_list = sorted(files_edited)[:MAX_FILES_PER_SESSION]

    # Deduplicate while preserving order
    def uniq(seq):
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return {
        "session_id": path.stem,
        "path": str(path),
        "cwd": cwd or path.parent.name,
        "git_branch": git_branch,
        "first_ts_utc": first_ts.isoformat() if first_ts else None,
        "last_ts_utc": last_ts.isoformat() if last_ts else None,
        "first_ts_local": first_ts.astimezone().isoformat() if first_ts else None,
        "last_ts_local": last_ts.astimezone().isoformat() if last_ts else None,
        "user_msg_count": user_msg_count,
        "asst_msg_count": asst_msg_count,
        "prompts": prompts,
        "commands": commands,
        "files_edited": files_edited_list,
        "files_edited_total": len(files_edited),
        "files_read_count": len(files_read),
        "bash_descriptions": bash_descs,
        "agents_used": uniq(agents),
        "skills_used": uniq(skills),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (defaults to today in local tz)")
    args = ap.parse_args()

    if args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid --date: {args.date!r} (expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(2)
    else:
        target = logical_date(datetime.now().astimezone())

    sessions: list[dict] = []
    if PROJECTS_DIR.exists():
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl in project_dir.glob("*.jsonl"):
                try:
                    mtime_logical = logical_date(
                        datetime.fromtimestamp(jsonl.stat().st_mtime).astimezone()
                    )
                except OSError:
                    continue
                if mtime_logical < target:
                    continue
                result = process_session(jsonl, target)
                if result:
                    sessions.append(result)

    sessions.sort(key=lambda s: s["first_ts_utc"] or "")

    projects = {}
    for s in sessions:
        projects.setdefault(s["cwd"], []).append(s["session_id"])

    out = {
        "date": target.isoformat(),
        "session_count": len(sessions),
        "project_count": len(projects),
        "projects": projects,
        "sessions": sessions,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
