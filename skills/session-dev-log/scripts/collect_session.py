#!/usr/bin/env python3
"""Collect a single Claude Code session's conversation as ordered turns.

Finds the current (or specified) session JSONL, then emits the dialogue as an
ordered list of turns: each human turn keeps the user's text verbatim; each AI
turn carries the assistant's prose plus a summary of tool calls. The
session-dev-log skill consumes this JSON to compose a 티키타카-style markdown
summary (human verbatim, AI summarized).

Session selection: by default the most-recently-active JSONL under the cwd's
project dir — which is the live session, because running this script itself
appends to it. Override with --session / --file / --cwd.

Output: JSON to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Sessions started before this local hour are attributed to the previous
# calendar day (late-night work continuation). 01:30 next-day becomes the
# previous day's "25-30" slot — the wall-clock hour + 24 — so files sort after
# the evening sessions they continue.
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

MAX_HUMAN_CHARS = 6000      # human text kept verbatim — generous cap
MAX_AI_TEXT_CHARS = 5000    # assistant prose per turn (Claude summarizes it)
MAX_TOOLS_PER_TURN = 80


def encode_cwd(path: str) -> str:
    """Mirror Claude Code's project-dir encoding: non-alphanumeric -> '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def logical_date(ts):
    """Date the user would consider this timestamp to 'belong to'.

    Past-midnight hours (00:00 to LATE_NIGHT_CUTOFF_HOUR - 1) are folded into
    the previous calendar day.
    """
    if ts is None:
        return None
    local = ts.astimezone()
    if local.hour < LATE_NIGHT_CUTOFF_HOUR:
        return (local - timedelta(days=1)).date()
    return local.date()


def display_hhmm(ts):
    """HH-MM string for filenames. Past-midnight hours use 24+ (01:30 -> 25-30)."""
    if ts is None:
        return None
    local = ts.astimezone()
    hour = local.hour + 24 if local.hour < LATE_NIGHT_CUTOFF_HOUR else local.hour
    return f"{hour:02d}-{local.minute:02d}"


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def classify_user_message(msg):
    content = msg.get("message", {}).get("content", "")
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
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
            summary = inp.get("file_path", "")
        elif name == "Bash":
            summary = inp.get("description") or (inp.get("command", "")[:100])
        elif name == "Agent":
            summary = inp.get("description", "") or inp.get("subagent_type", "")
        elif name == "Skill":
            summary = inp.get("skill", "")
        elif name.startswith("Task"):
            summary = inp.get("description", "")
        elif name == "WebFetch":
            summary = inp.get("url", "")
        elif name == "WebSearch":
            summary = inp.get("query", "")
        elif name in ("Glob", "Grep"):
            summary = inp.get("pattern", "")
        else:
            summary = ""
        out.append({"name": name, "summary": summary})
    return out


def find_session_file(args) -> Path | None:
    if args.file:
        p = Path(args.file).expanduser()
        return p if p.is_file() else None
    cwd = args.cwd or os.getcwd()
    proj = PROJECTS_DIR / encode_cwd(cwd)
    if not proj.is_dir():
        return None
    candidates = list(proj.glob("*.jsonl"))
    if not candidates:
        return None
    if args.session:
        for c in candidates:
            if c.stem == args.session:
                return c
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def process(path: Path) -> dict:
    cwd = None
    git_branch = None
    first_ts = None
    last_ts = None
    turns: list[dict] = []
    ai_text_parts: list[str] = []
    ai_tools: list[dict] = []
    ai_ts = None
    human_count = 0
    ai_msg_count = 0

    def flush_ai():
        nonlocal ai_text_parts, ai_tools, ai_ts
        if not ai_text_parts and not ai_tools:
            return
        text = "\n".join(p for p in ai_text_parts if p).strip()
        turns.append(
            {
                "role": "ai",
                "ts": ai_ts,
                "text": text[:MAX_AI_TEXT_CHARS],
                "text_truncated": len(text) > MAX_AI_TEXT_CHARS,
                "tools": ai_tools[:MAX_TOOLS_PER_TURN],
                "tool_count": len(ai_tools),
            }
        )
        ai_text_parts = []
        ai_tools = []
        ai_ts = None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("isSidechain"):
                continue  # subagent transcript — not part of the main dialogue

            ts = parse_ts(obj.get("timestamp"))
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            if obj.get("cwd") and not cwd:
                cwd = obj["cwd"]
            if obj.get("gitBranch") and not git_branch:
                git_branch = obj["gitBranch"]

            t = obj.get("type")
            if t == "user":
                kind, text = classify_user_message(obj)
                if kind in ("prompt", "command") and text:
                    flush_ai()
                    human_count += 1
                    turns.append(
                        {
                            "role": "human",
                            "kind": kind,
                            "ts": obj.get("timestamp"),
                            "text": text[:MAX_HUMAN_CHARS],
                            "text_truncated": len(text) > MAX_HUMAN_CHARS,
                        }
                    )
                # tool_result / meta user messages: ignore
            elif t == "assistant":
                ai_msg_count += 1
                if ai_ts is None:
                    ai_ts = obj.get("timestamp")
                atext = extract_text(obj.get("message", {}).get("content", []))
                if atext.strip():
                    ai_text_parts.append(atext.strip())
                ai_tools.extend(extract_tool_uses(obj))

    flush_ai()

    logical = logical_date(first_ts)
    calendar = first_ts.astimezone().date() if first_ts else None
    return {
        "session_id": path.stem,
        "path": str(path),
        "cwd": cwd or path.parent.name,
        "git_branch": git_branch,
        "logical_date": logical.isoformat() if logical else None,
        "calendar_date": calendar.isoformat() if calendar else None,
        "display_hhmm": display_hhmm(first_ts),
        "late_night": bool(logical and calendar and logical != calendar),
        "first_ts_local": first_ts.astimezone().isoformat() if first_ts else None,
        "last_ts_local": last_ts.astimezone().isoformat() if last_ts else None,
        "human_turn_count": human_count,
        "ai_msg_count": ai_msg_count,
        "turn_count": len(turns),
        "turns": turns,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="session id (JSONL stem) to summarize")
    ap.add_argument("--file", help="explicit path to a session JSONL")
    ap.add_argument("--cwd", help="project cwd to resolve (defaults to current dir)")
    args = ap.parse_args()

    path = find_session_file(args)
    if path is None:
        print(
            json.dumps(
                {"error": "no session JSONL found", "cwd": args.cwd or os.getcwd()},
                ensure_ascii=False,
            )
        )
        sys.exit(1)

    print(json.dumps(process(path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
