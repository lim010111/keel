#!/usr/bin/env python3
"""Generate a daily Claude Code token-usage report as a self-contained HTML file.

Scope matches the daily-dev-log skill: a "day" is [target 05:00, target+1 05:00)
in local time. Walks ~/.claude/projects/*/<sessionId>.jsonl and counts token
usage from every assistant message whose timestamp falls on the target *logical*
date — per-message attribution, so a session spanning two days is split
correctly.

Two-phase task breakdown:
  --collect-tasks  emits every task (one user request = one task) as JSON
                   on stdout, so an agent can write Korean one-line labels.
  --labels FILE    renders the HTML using {task_id: "label"} from FILE; any
                   task without a label falls back to a truncated prompt.

Run with neither flag to render in one shot with truncated-prompt labels.
Refuses to overwrite an existing file unless --force is given.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OBSIDIAN_DEVLOG = Path("/mnt/c/Users/shine/Documents/Obsidian/0. Daily/Dev log")

# Messages before this local hour count toward the previous calendar day.
LATE_NIGHT_CUTOFF_HOUR = 5

# A git worktree created by Claude Code lives at <project>/.claude/worktrees/
# <name>; splitting a cwd here groups worktree sessions under their project.
WORKTREE_MARKER = "/.claude/worktrees/"

# --- Model pricing (USD per 1M tokens) -------------------------------------
# EDIT HERE when Anthropic pricing changes. A model id is matched by substring
# (lower-cased): the first family whose key appears in the id wins.
#   input       = fresh input tokens
#   output      = generated tokens
#   cache_write = cache creation (5-minute TTL); ~1.25x input
#   cache_read  = cache hits;                    ~0.10x input
PRICING = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
}

# Visual order for the hourly chart: a logical day runs 05:00 -> next 04:59.
HOUR_ORDER = list(range(5, 24)) + list(range(0, 5))

TOKEN_KINDS = [
    ("input", "입력", "#4f7cff"),
    ("output", "출력", "#ff7a59"),
    ("cache_create", "캐시 생성", "#9b7bff"),
    ("cache_read", "캐시 읽기", "#33b8a6"),
]

MAX_PROMPT_CHARS = 400
MAX_TASK_FILES = 8
MAX_TASK_BASH = 6

COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")
COMMAND_ARGS_RE = re.compile(r"<command-args>([^<]*)</command-args>", re.DOTALL)
META_BLOCK_RE = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-caveat|command-message)>"
    r".*?</\1>",
    re.DOTALL,
)


# --- date / parsing helpers -------------------------------------------------
def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def logical_date(ts):
    """Local calendar date the timestamp is attributed to (late-night rule)."""
    if ts is None:
        return None
    local = ts.astimezone()
    if local.hour < LATE_NIGHT_CUTOFF_HOUR:
        return (local - timedelta(days=1)).date()
    return local.date()


def model_family(model_id):
    mid = (model_id or "").lower()
    for family in PRICING:
        if family in mid:
            return family
    return None


def split_worktree(cwd):
    """Return (parent_project_cwd, worktree_name|None) for a session cwd."""
    i = cwd.find(WORKTREE_MARKER)
    if i == -1:
        return cwd, None
    return cwd[:i], cwd[i + len(WORKTREE_MARKER):].split("/", 1)[0]


def message_cost(family, tok):
    p = PRICING.get(family)
    if not p:
        return 0.0
    return (
        tok["input"] * p["input"]
        + tok["output"] * p["output"]
        + tok["cache_create"] * p["cache_write"]
        + tok["cache_read"] * p["cache_read"]
    ) / 1_000_000


# --- user-message classification (task boundaries) -------------------------
def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def classify_user_message(obj):
    """Return (kind, text). kind in prompt|command|tool_result|meta.

    A `prompt` or `command` starts a new task; `tool_result`/`meta` do not.
    """
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return "tool_result", ""
    text = extract_text(content)
    if "<command-name>" in text:
        m = COMMAND_NAME_RE.search(text)
        a = COMMAND_ARGS_RE.search(text)
        cmd = (m.group(1) if m else "").strip().lstrip("/")
        args = (a.group(1) if a else "").strip()
        return "command", f"/{cmd} {args}".strip()
    if obj.get("isMeta"):
        return "meta", ""
    # Strip harness-injected meta blocks; a real prompt has text left over.
    cleaned = META_BLOCK_RE.sub("", text)
    cleaned = re.sub(r"^\[Request interrupted.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    if not cleaned:
        return "meta", ""
    return "prompt", cleaned


def extract_tool_uses(obj):
    """Yield (tool_name, summary) for each tool_use block in an assistant msg."""
    content = obj.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input", {}) or {}
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
            summary = inp.get("file_path", "")
        elif name == "Bash":
            summary = inp.get("description") or inp.get("command", "")[:80]
        elif name == "Agent":
            summary = inp.get("description", "") or inp.get("subagent_type", "")
        elif name == "Skill":
            summary = inp.get("skill", "")
        else:
            summary = ""
        yield name, summary


# --- accumulator ------------------------------------------------------------
class Acc:
    __slots__ = ("input", "output", "cache_create", "cache_read", "cost", "msgs")

    def __init__(self):
        self.input = self.output = self.cache_create = self.cache_read = 0
        self.cost = 0.0
        self.msgs = 0

    def add(self, tok, cost):
        self.input += tok["input"]
        self.output += tok["output"]
        self.cache_create += tok["cache_create"]
        self.cache_read += tok["cache_read"]
        self.cost += cost
        self.msgs += 1

    def add_acc(self, other):
        self.input += other.input
        self.output += other.output
        self.cache_create += other.cache_create
        self.cache_read += other.cache_read
        self.cost += other.cost
        self.msgs += other.msgs

    @property
    def total(self):
        return self.input + self.output + self.cache_create + self.cache_read


def new_task(tid, prompt, session, cwd):
    return {
        "id": tid,
        "prompt": prompt,
        "session": session,
        "cwd": cwd,
        "acc": Acc(),
        "first_ts": None,
        "files": [],
        "skills": [],
        "agents": [],
        "bash": [],
    }


# --- collection -------------------------------------------------------------
def collect(target):
    """Scan all session files; return aggregated token usage for `target`."""
    totals = Acc()
    projects = defaultdict(Acc)
    models = defaultdict(Acc)
    hours = defaultdict(Acc)
    sessions = {}  # session_id -> dict(acc, cwd, first, last)
    tasks = []     # one entry per user request that consumed target-day tokens
    unknown_models = set()
    seen_msg_ids = set()
    first_ts = last_ts = None

    if not PROJECTS_DIR.exists():
        return None

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(jsonl.stat().st_mtime).astimezone()
            except OSError:
                continue
            if logical_date(mtime) < target:
                continue  # last activity predates the target day

            sid = jsonl.stem
            file_cwd = None
            sess_acc = Acc()
            sess_first = sess_last = None
            current_task = None
            orphan_task = None
            file_ai_title = None
            file_custom_title = None

            try:
                with open(jsonl, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if not file_cwd and obj.get("cwd"):
                            file_cwd = obj["cwd"]
                        t = obj.get("type")

                        # Session title — written to the JSONL by Claude Code;
                        # custom (user-set) overrides ai-generated. Last wins.
                        if t == "ai-title":
                            if obj.get("aiTitle"):
                                file_ai_title = obj["aiTitle"]
                            continue
                        if t == "custom-title":
                            if obj.get("customTitle"):
                                file_custom_title = obj["customTitle"]
                            continue

                        # --- task boundary: a real (non-sidechain) request ---
                        if t == "user":
                            if obj.get("isSidechain"):
                                continue  # subagent's own prompt — not a task
                            kind, text = classify_user_message(obj)
                            if kind in ("prompt", "command") and text:
                                tid = obj.get("uuid") or f"{sid}:{len(tasks)}"
                                current_task = new_task(
                                    tid,
                                    text[:MAX_PROMPT_CHARS],
                                    sid,
                                    obj.get("cwd") or file_cwd or sid,
                                )
                                tasks.append(current_task)
                            continue

                        if t != "assistant":
                            continue

                        msg = obj.get("message") or {}

                        # tool_use blocks describe what the task did (label aid)
                        if current_task is not None:
                            for name, summary in extract_tool_uses(obj):
                                if not summary:
                                    continue
                                if name in ("Edit", "Write", "MultiEdit",
                                            "NotebookEdit"):
                                    if (summary not in current_task["files"]
                                            and len(current_task["files"])
                                            < MAX_TASK_FILES):
                                        current_task["files"].append(summary)
                                elif name == "Bash":
                                    if len(current_task["bash"]) < MAX_TASK_BASH:
                                        current_task["bash"].append(summary)
                                elif name == "Agent":
                                    if summary not in current_task["agents"]:
                                        current_task["agents"].append(summary)
                                elif name == "Skill":
                                    if summary not in current_task["skills"]:
                                        current_task["skills"].append(summary)

                        usage = msg.get("usage")
                        if not usage:
                            continue
                        ts = parse_ts(obj.get("timestamp"))
                        if logical_date(ts) != target:
                            continue

                        msg_id = msg.get("id") or obj.get("uuid")
                        if msg_id in seen_msg_ids:
                            continue
                        if msg_id:
                            seen_msg_ids.add(msg_id)

                        tok = {
                            "input": usage.get("input_tokens", 0) or 0,
                            "output": usage.get("output_tokens", 0) or 0,
                            "cache_create": usage.get("cache_creation_input_tokens", 0) or 0,
                            "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                        }
                        if sum(tok.values()) == 0:
                            continue

                        model_id = msg.get("model") or "(unknown)"
                        family = model_family(model_id)
                        if family is None:
                            unknown_models.add(model_id)
                        cost = message_cost(family, tok)
                        local = ts.astimezone()

                        totals.add(tok, cost)
                        models[model_id].add(tok, cost)
                        sess_acc.add(tok, cost)
                        hours[local.hour].add(tok, cost)

                        # attribute to the current task (or a per-file orphan)
                        task = current_task
                        if task is None:
                            if orphan_task is None:
                                orphan_task = new_task(
                                    f"{sid}:orphan", "(요청 경계 불명)", sid,
                                    file_cwd or sid)
                                tasks.append(orphan_task)
                            task = orphan_task
                        task["acc"].add(tok, cost)
                        if task["first_ts"] is None or local < task["first_ts"]:
                            task["first_ts"] = local

                        if sess_first is None or local < sess_first:
                            sess_first = local
                        if sess_last is None or local > sess_last:
                            sess_last = local
                        if first_ts is None or local < first_ts:
                            first_ts = local
                        if last_ts is None or local > last_ts:
                            last_ts = local
            except OSError:
                continue

            if sess_acc.msgs == 0:
                continue
            cwd = file_cwd or project_dir.name
            parent, worktree = split_worktree(cwd)
            projects[parent].add_acc(sess_acc)
            sessions[sid] = {
                "acc": sess_acc,
                "cwd": cwd,
                "parent": parent,
                "worktree": worktree,
                "first": sess_first,
                "last": sess_last,
                "title": file_custom_title or file_ai_title,
            }

    # Keep only tasks that actually consumed target-day tokens; stable sort.
    tasks = [t for t in tasks if t["acc"].total > 0]
    tasks.sort(key=lambda t: (-t["acc"].total, t["id"]))

    return {
        "totals": totals,
        "projects": dict(projects),
        "models": dict(models),
        "hours": dict(hours),
        "sessions": sessions,
        "tasks": tasks,
        "unknown_models": sorted(unknown_models),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


# --- formatting helpers -----------------------------------------------------
def fmt_int(n):
    return f"{n:,}"


def fmt_cost(c):
    if c <= 0:
        return "$0.00"
    if c < 0.01:
        return f"${c:.4f}"
    return f"${c:,.2f}"


def fmt_pct(part, whole):
    return f"{(100.0 * part / whole):.1f}%" if whole else "0.0%"


def alias(cwd):
    name = Path(cwd).name or cwd
    return "home" if cwd == str(Path.home()) else name


def esc(s):
    return html.escape(str(s))


def bar(pct, color):
    return (
        f'<div class="bar"><div class="bar-fill" '
        f'style="width:{pct:.1f}%;background:{color}"></div></div>'
    )


def fallback_label(prompt):
    s = " ".join(prompt.split())
    return s[:80] + ("…" if len(s) > 80 else "") if s else "(빈 요청)"


# --- HTML rendering ---------------------------------------------------------
def render_html(target, data, labels):
    totals = data["totals"]
    t = totals.total
    tr = ""
    if data["first_ts"] and data["last_ts"]:
        tr = f' · {data["first_ts"]:%H:%M} ~ {data["last_ts"]:%H:%M} (KST)'

    busiest_proj = max(
        data["projects"].items(), key=lambda kv: kv[1].total, default=None
    )
    busiest_hour = max(data["hours"].items(), key=lambda kv: kv[1].total, default=None)

    out = []
    out.append(f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{target} 토큰 사용 리포트</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, "Noto Sans KR", sans-serif;
    background: #f4f5f7; color: #1c1e21; line-height: 1.5; padding: 32px 16px; }}
  .wrap {{ max-width: 960px; margin: 0 auto; }}
  header {{ background: linear-gradient(135deg, #4f7cff, #9b7bff);
    color: #fff; border-radius: 16px; padding: 28px 32px; margin-bottom: 24px; }}
  header h1 {{ font-size: 24px; font-weight: 700; }}
  header .sub {{ opacity: .9; font-size: 14px; margin-top: 6px; }}
  section {{ background: #fff; border-radius: 14px; padding: 22px 24px;
    margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  h2 {{ font-size: 15px; font-weight: 700; margin-bottom: 16px; color: #34373c;
    letter-spacing: .01em; }}
  .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }}
  .card {{ background: #fff; border-radius: 14px; padding: 18px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .card .label {{ font-size: 12px; color: #6b7280; font-weight: 600; }}
  .card .value {{ font-size: 26px; font-weight: 700; margin-top: 6px; }}
  .card .note {{ font-size: 12px; color: #9ca3af; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: right; padding: 8px 10px; }}
  th {{ color: #6b7280; font-weight: 600; border-bottom: 2px solid #eceef1;
    font-size: 12px; }}
  td {{ border-bottom: 1px solid #f0f1f3; }}
  th:first-child, td:first-child {{ text-align: left; }}
  tbody tr:hover {{ background: #fafbfc; }}
  .name {{ font-weight: 600; }}
  .mono {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
    color: #6b7280; }}
  .tasklabel {{ font-weight: 600; max-width: 380px; }}
  .tasklabel small, .sesslabel small {{ display: block; font-weight: 400;
    color: #9ca3af; font-size: 11px; margin-top: 1px; }}
  .bar {{ background: #eef0f3; border-radius: 5px; height: 9px; width: 100%;
    overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 5px; }}
  .barcell {{ width: 140px; }}
  .stack {{ display: flex; height: 22px; border-radius: 6px; overflow: hidden;
    margin-bottom: 14px; }}
  .stack span {{ display: block; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px;
    color: #4b5563; }}
  .legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 3px;
    margin-right: 5px; vertical-align: middle; }}
  .grand td {{ font-weight: 700; border-top: 2px solid #eceef1; border-bottom: none; }}
  .hour td {{ padding: 4px 10px; }}
  .hour .hlabel {{ width: 52px; color: #6b7280; font-variant-numeric: tabular-nums; }}
  footer {{ font-size: 12px; color: #9ca3af; text-align: center; padding: 8px 0 4px; }}
  footer code {{ background: #ececf0; padding: 1px 5px; border-radius: 4px; }}
  .pill {{ display: inline-block; background: #eef1ff; color: #4f7cff;
    border-radius: 999px; padding: 2px 9px; font-size: 11px; font-weight: 600; }}
  .ptoggle {{ cursor: pointer; }}
  .ptoggle .name {{ user-select: none; }}
  .caret {{ display: inline-block; width: 14px; color: #9ca3af; font-size: 10px; }}
  .caret::before {{ content: "\\25B8"; }}
  .ptoggle.open .caret::before {{ content: "\\25BE"; }}
  tr.child {{ display: none; }}
  tr.child.show {{ display: table-row; }}
  td.sub {{ padding-left: 26px; }}
  td.sub2 {{ padding-left: 48px; }}
  .wt {{ display: inline-block; background: #fff3e6; color: #d97a2b;
    border-radius: 4px; padding: 0 5px; font-size: 10px; font-weight: 600; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>🎟️ {target} 토큰 사용 리포트</h1>
  <div class="sub">Claude Code 세션 {len(data['sessions'])}개 · 프로젝트 {len(data['projects'])}개{tr}</div>
</header>
""")

    # KPI cards
    # Cache reuse ratio = cache reads / all tokens that passed through the
    # cache (reads + writes). Uncached input_tokens is excluded — this matches
    # Anthropic's "cache read ratio" dashboard metric.
    cacheable = totals.cache_read + totals.cache_create
    reuse = fmt_pct(totals.cache_read, cacheable)
    out.append('<div class="cards">')
    out.append(f'<div class="card"><div class="label">총 토큰</div>'
               f'<div class="value">{fmt_int(t)}</div>'
               f'<div class="note">메시지 {fmt_int(totals.msgs)}건</div></div>')
    out.append(f'<div class="card"><div class="label">추정 비용</div>'
               f'<div class="value">{fmt_cost(totals.cost)}</div>'
               f'<div class="note">USD · 가격표 기준 추정</div></div>')
    out.append(f'<div class="card"><div class="label">캐시 재사용 비율</div>'
               f'<div class="value">{reuse}</div>'
               f'<div class="note">캐시 토큰 중 읽기 비중</div></div>')
    avg = totals.cost / len(data["sessions"]) if data["sessions"] else 0.0
    out.append(f'<div class="card"><div class="label">세션당 평균</div>'
               f'<div class="value">{fmt_cost(avg)}</div>'
               f'<div class="note">세션 {len(data["sessions"])}개 기준</div></div>')
    out.append('</div>')

    # Token composition
    out.append('<section><h2>토큰 구성</h2>')
    if t > 0:
        out.append('<div class="stack">')
        for key, _, color in TOKEN_KINDS:
            v = getattr(totals, key)
            if v:
                out.append(f'<span style="width:{100.0*v/t:.2f}%;background:{color}"></span>')
        out.append('</div>')
    out.append('<div class="legend">')
    for key, label, color in TOKEN_KINDS:
        out.append(f'<span><i style="background:{color}"></i>{label}</span>')
    out.append('</div>')
    out.append('<table style="margin-top:14px"><thead><tr>'
               '<th>종류</th><th>토큰</th><th>비중</th><th>추정 비용</th>'
               '</tr></thead><tbody>')
    for key, label, color in TOKEN_KINDS:
        v = getattr(totals, key)
        out.append(f'<tr><td class="name"><i style="display:inline-block;width:9px;'
                   f'height:9px;border-radius:2px;background:{color};margin-right:6px">'
                   f'</i>{label}</td><td>{fmt_int(v)}</td>'
                   f'<td>{fmt_pct(v, t)}</td><td>—</td></tr>')
    out.append(f'<tr class="grand"><td>합계</td><td>{fmt_int(t)}</td>'
               f'<td>100%</td><td>{fmt_cost(totals.cost)}</td></tr>')
    out.append('</tbody></table></section>')

    # Per-project tree: project -> session -> task. One user request = one
    # task; tasks nest under the session they ran in, sessions under their
    # parent project. Worktree sessions nest under the parent project with a
    # wt:<name> badge. Every level is collapsed by default.
    proj_sessions = defaultdict(list)
    for sid, s in data["sessions"].items():
        proj_sessions[s["parent"]].append((sid, s))
    sess_tasks = defaultdict(list)
    for tk in data["tasks"]:
        sess_tasks[tk["session"]].append(tk)
    proj_rows = sorted(data["projects"].items(),
                       key=lambda kv: kv[1].cost, reverse=True)
    pmax = max((a.total for _, a in proj_rows), default=1) or 1
    out.append('<section><h2>프로젝트별 사용량 '
               '<span class="pill">행을 누르면 세션·작업이 펼쳐집니다</span></h2>')
    out.append('<table><thead><tr><th>프로젝트 / 세션 / 작업</th><th>메시지</th>'
               '<th>입력</th><th>출력</th><th>캐시</th><th>총 토큰</th>'
               '<th class="barcell"></th><th>추정 비용</th>'
               '</tr></thead><tbody>')
    for pi, (cwd, pa) in enumerate(proj_rows):
        pg = f"p{pi}"
        sess = sorted(proj_sessions.get(cwd, []),
                      key=lambda kv: kv[1]["acc"].total, reverse=True)
        out.append(
            f'<tr class="ptoggle" data-g="{pg}">'
            f'<td class="name"><span class="caret"></span>'
            f'<span title="{esc(cwd)}">{esc(alias(cwd))}</span> '
            f'<span class="pill">세션 {len(sess)}</span></td>'
            f'<td>{fmt_int(pa.msgs)}</td>'
            f'<td>{fmt_int(pa.input)}</td><td>{fmt_int(pa.output)}</td>'
            f'<td>{fmt_int(pa.cache_create + pa.cache_read)}</td>'
            f'<td>{fmt_int(pa.total)}</td>'
            f'<td class="barcell">{bar(100.0*pa.total/pmax, "#4f7cff")}</td>'
            f'<td>{fmt_cost(pa.cost)}</td></tr>')
        smax = pa.total or 1
        for si, (sid, s) in enumerate(sess):
            sg = f"{pg}s{si}"
            sa = s["acc"]
            stasks = sorted(sess_tasks.get(sid, []),
                            key=lambda tk: tk["acc"].total, reverse=True)
            title = s.get("title") or sid[:8]
            badge = (f' <span class="wt">wt:{esc(s["worktree"])}</span>'
                     if s["worktree"] else "")
            span = ""
            if s["first"] and s["last"]:
                span = f'{s["first"]:%H:%M}~{s["last"]:%H:%M} · '
            out.append(
                f'<tr class="child child-{pg} ptoggle" data-g="{sg}">'
                f'<td class="name sesslabel sub"><span class="caret"></span>'
                f'<span title="{esc(sid)}">{esc(title)}</span>{badge} '
                f'<span class="pill">작업 {len(stasks)}</span>'
                f'<small>{span}{esc(sid[:8])}</small></td>'
                f'<td>{fmt_int(sa.msgs)}</td>'
                f'<td>{fmt_int(sa.input)}</td><td>{fmt_int(sa.output)}</td>'
                f'<td>{fmt_int(sa.cache_create + sa.cache_read)}</td>'
                f'<td>{fmt_int(sa.total)}</td>'
                f'<td class="barcell">{bar(100.0*sa.total/smax, "#8ba6ff")}</td>'
                f'<td>{fmt_cost(sa.cost)}</td></tr>')
            tmax = sa.total or 1
            for tk in stasks:
                a = tk["acc"]
                label = labels.get(tk["id"]) or fallback_label(tk["prompt"])
                when = f'{tk["first_ts"]:%H:%M}' if tk["first_ts"] else "—"
                out.append(
                    f'<tr class="child child-{sg}">'
                    f'<td class="tasklabel sub2">{esc(label)}'
                    f'<small>{when}</small></td>'
                    f'<td>{fmt_int(a.msgs)}</td>'
                    f'<td>{fmt_int(a.input)}</td><td>{fmt_int(a.output)}</td>'
                    f'<td>{fmt_int(a.cache_create + a.cache_read)}</td>'
                    f'<td>{fmt_int(a.total)}</td>'
                    f'<td class="barcell">{bar(100.0*a.total/tmax, "#bcc9f5")}</td>'
                    f'<td>{fmt_cost(a.cost)}</td></tr>')
    out.append(f'<tr class="grand"><td>합계</td><td>{fmt_int(totals.msgs)}</td>'
               f'<td>{fmt_int(totals.input)}</td><td>{fmt_int(totals.output)}</td>'
               f'<td>{fmt_int(totals.cache_create + totals.cache_read)}</td>'
               f'<td>{fmt_int(t)}</td><td></td>'
               f'<td>{fmt_cost(totals.cost)}</td></tr>')
    out.append('</tbody></table></section>')

    # Per-model
    model_rows = sorted(data["models"].items(), key=lambda kv: kv[1].cost, reverse=True)
    out.append('<section><h2>모델별 사용량</h2>')
    out.append('<table><thead><tr><th>모델</th><th>메시지</th><th>총 토큰</th>'
               '<th>비중</th><th>추정 비용</th></tr></thead><tbody>')
    for mid, a in model_rows:
        known = model_family(mid) is not None
        cost_cell = fmt_cost(a.cost) if known else '<span class="pill">가격 미상</span>'
        out.append(f'<tr><td class="mono">{esc(mid)}</td><td>{fmt_int(a.msgs)}</td>'
                   f'<td>{fmt_int(a.total)}</td><td>{fmt_pct(a.total, t)}</td>'
                   f'<td>{cost_cell}</td></tr>')
    out.append('</tbody></table></section>')

    # Hourly distribution
    hmax = max((a.total for a in data["hours"].values()), default=1) or 1
    out.append('<section><h2>시간대별 분포</h2><table><tbody>')
    for h in HOUR_ORDER:
        a = data["hours"].get(h)
        tot = a.total if a else 0
        cost = a.cost if a else 0.0
        out.append(
            f'<tr class="hour"><td class="hlabel">{h:02d}시</td>'
            f'<td class="barcell">{bar(100.0*tot/hmax, "#9b7bff")}</td>'
            f'<td>{fmt_int(tot)}</td><td>{fmt_cost(cost)}</td></tr>')
    out.append('</tbody></table></section>')

    # Highlights + footer
    hi = []
    if busiest_proj:
        hi.append(f'토큰을 가장 많이 쓴 프로젝트는 <b>{esc(alias(busiest_proj[0]))}</b>'
                  f'({fmt_int(busiest_proj[1].total)} 토큰)')
    if data["tasks"]:
        big = data["tasks"][0]
        biglabel = labels.get(big["id"]) or fallback_label(big["prompt"])
        hi.append(f'가장 무거운 작업은 <b>{esc(biglabel)}</b>'
                  f'({fmt_int(big["acc"].total)} 토큰)')
    if busiest_hour:
        hi.append(f'가장 활발한 시간대는 <b>{busiest_hour[0]:02d}시</b>'
                  f'({fmt_int(busiest_hour[1].total)} 토큰)')
    if hi:
        out.append(f'<section><h2>한눈에</h2><p style="font-size:13px;color:#4b5563">'
                   f'{" · ".join(hi)}.</p></section>')

    unknown = ""
    if data["unknown_models"]:
        unknown = (" · 가격 미상 모델: "
                   + ", ".join(esc(m) for m in data["unknown_models"]))
    out.append(
        f'<footer>생성: {datetime.now().astimezone():%Y-%m-%d %H:%M} (KST) · '
        f'비용은 스크립트의 <code>PRICING</code> 표 기준 추정치이며 실제 청구액과 '
        f'다를 수 있습니다{unknown}</footer>')
    out.append("""<script>
function toggleRow(row) {
  var g = row.getAttribute("data-g");
  var open = row.classList.toggle("open");
  document.querySelectorAll("tr.child-" + g).forEach(function (c) {
    c.classList.toggle("show", open);
    // Collapsing a parent must also collapse any open descendant toggle.
    if (!open && c.classList.contains("ptoggle") && c.classList.contains("open")) {
      toggleRow(c);
    }
  });
}
document.querySelectorAll("tr.ptoggle").forEach(function (row) {
  row.addEventListener("click", function () { toggleRow(row); });
});
</script>""")
    out.append('</div></body></html>')
    return "\n".join(out)


# --- task JSON for the labelling phase --------------------------------------
def tasks_payload(target, data):
    items = []
    for tk in data["tasks"]:
        parent, wt = split_worktree(tk["cwd"])
        sess = data["sessions"].get(tk["session"], {})
        items.append({
            "id": tk["id"],
            "project": alias(parent),
            "worktree": wt,
            "session": tk["session"][:8],
            "session_title": sess.get("title"),
            "time": f'{tk["first_ts"]:%H:%M}' if tk["first_ts"] else None,
            "tokens": tk["acc"].total,
            "cost": round(tk["acc"].cost, 4),
            "messages": tk["acc"].msgs,
            "prompt": tk["prompt"],
            "files_edited": tk["files"],
            "skills": tk["skills"],
            "agents": tk["agents"],
            "bash": tk["bash"],
        })
    return {
        "date": target.isoformat(),
        "task_count_total": len(items),
        "note": "각 task의 id별로 한 줄 한국어 요약을 만들어 "
                "{task_id: \"요약\"} 형태의 labels.json으로 저장하세요.",
        "tasks": items,
    }


# --- main -------------------------------------------------------------------
def default_out(target):
    d = target
    return (OBSIDIAN_DEVLOG / f"{d:%Y}" / f"{d:%Y-%m}" / f"{d:%Y-%m-%d}"
            / f"{d:%Y-%m-%d}-tokens.html")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘, 로컬 타임존)")
    ap.add_argument("--out", help="출력 HTML 경로 (기본: daily-dev-log 날짜 폴더)")
    ap.add_argument("--force", action="store_true", help="기존 파일 덮어쓰기")
    ap.add_argument("--collect-tasks", action="store_true",
                    help="HTML 대신 라벨링용 작업 목록 JSON을 stdout으로 출력")
    ap.add_argument("--labels", help="{task_id: 요약} JSON 파일 경로 (작업 라벨)")
    args = ap.parse_args()

    if args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"잘못된 --date: {args.date!r} (YYYY-MM-DD 형식)", file=sys.stderr)
            sys.exit(2)
    else:
        target = logical_date(datetime.now().astimezone())

    data = collect(target)
    if data is None:
        print(f"세션 디렉토리를 찾을 수 없습니다: {PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    if args.collect_tasks:
        print(json.dumps(tasks_payload(target, data),
                         ensure_ascii=False, indent=2))
        return

    if data["totals"].msgs == 0:
        print(f"{target}에 토큰 사용 활동이 없습니다.")
        sys.exit(0)

    labels = {}
    if args.labels:
        try:
            labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"라벨 파일을 읽지 못했습니다 ({e}). 프롬프트 자동 라벨로 대체합니다.",
                  file=sys.stderr)

    out_path = Path(args.out) if args.out else default_out(target)
    if out_path.exists() and not args.force:
        print(f"이미 존재함: {out_path}\n덮어쓰려면 --force 를 붙여 다시 실행하세요.",
              file=sys.stderr)
        sys.exit(3)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(target, data, labels),
                        encoding="utf-8")

    totals = data["totals"]
    print(f"날짜:      {target}")
    print(f"세션:      {len(data['sessions'])}개 · 프로젝트 {len(data['projects'])}개"
          f" · 작업 {len(data['tasks'])}건")
    print(f"총 토큰:   {fmt_int(totals.total)} (메시지 {fmt_int(totals.msgs)}건)")
    print(f"추정 비용: {fmt_cost(totals.cost)} USD")
    print(f"저장 경로: {out_path}")


if __name__ == "__main__":
    main()
