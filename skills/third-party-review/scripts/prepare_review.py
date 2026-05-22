#!/usr/bin/env python3
"""Prepare third-party-review inputs.

Locates the current Claude Code session JSONL, reduces it deterministically
(structural cleanup A1-A4, then staged reduction S1-S6 only if over budget),
writes the reduced transcript and the assembled evaluator prompt into
<cwd>/.tpr/, and prints a JSON summary to stdout.

Usage: prepare_review.py [--session <id>] [--jsonl <path>]
"""
import glob
import json
import os
import re
import sys

SYSREMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SKILL_DIR)
import reduction_config as cfg  # noqa: E402

CWD = os.getcwd()
OUT_DIR = os.path.join(CWD, ".tpr")


# --- locate session JSONL ------------------------------------------------
def find_session_jsonl(session=None, jsonl=None):
    if jsonl:
        return jsonl
    projects = os.path.expanduser("~/.claude/projects")
    enc = CWD.replace("/", "-").replace(".", "-")
    proj_dir = os.path.join(projects, enc)
    candidates = glob.glob(os.path.join(proj_dir, "*.jsonl"))
    if not candidates:
        candidates = glob.glob(os.path.join(projects, "*", "*.jsonl"))
    if not candidates:
        raise SystemExit("prepare_review: no session JSONL found")
    if session:
        for c in candidates:
            if os.path.basename(c).startswith(session):
                return c
        raise SystemExit(f"prepare_review: session {session} not found")
    return max(candidates, key=os.path.getmtime)  # newest = current session


# --- parse JSONL into chronological events (A1-A3 cleanup) ---------------
def load_events(path):
    raw = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    tool_names, tool_inputs = {}, {}
    for o in raw:
        m = o.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_names[b.get("id")] = b.get("name", "?")
                    tool_inputs[b.get("id")] = b.get("input", {})

    events, human_n = [], 0
    for o in raw:
        if o.get("type") in ("file-history-snapshot", "summary"):
            continue                                    # A1
        if o.get("isMeta") or o.get("isCompactSummary"):
            continue                                    # A1, A2
        if o.get("isSidechain"):
            continue                                    # A3
        m = o.get("message")
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        # A user message is counted as ONE human turn even if it carries
        # several text blocks; <system-reminder> injections are harness
        # noise and are stripped out.
        human_parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = b.get("text", "")
                if role == "user":
                    txt = SYSREMINDER.sub("", txt).strip()
                    if txt:
                        human_parts.append(txt)
                elif txt.strip():
                    events.append({"kind": "ai_text", "text": txt})
            elif bt == "thinking":
                th = b.get("thinking", "")
                events.append({"kind": "thinking", "text": th,
                               "orig_len": len(th)})
            elif bt == "tool_use":
                events.append({"kind": "tool_use", "name": b.get("name", "?"),
                               "input": b.get("input", {})})
            elif bt == "tool_result":
                tid = b.get("tool_use_id")
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c
                                  if isinstance(x, dict)
                                  and x.get("type") == "text")
                elif not isinstance(c, str):
                    c = json.dumps(c, ensure_ascii=False)
                events.append({
                    "kind": "tool_result",
                    "name": tool_names.get(tid, "?"),
                    "path": tool_inputs.get(tid, {}).get("file_path", "?"),
                    "text": c or "",
                    "is_error": bool(b.get("is_error")),
                })
        if human_parts:
            human_n += 1
            events.append({"kind": "human", "n": human_n,
                           "text": "\n\n".join(human_parts)})
    return events


# --- reduction helpers ---------------------------------------------------
def est_tokens(events):
    chars = 0
    for e in events:
        chars += len(e.get("text", ""))
        if e["kind"] == "tool_use":
            chars += len(json.dumps(e["input"], ensure_ascii=False))
    return chars // cfg.CHARS_PER_TOKEN


def clip_lines(text, head, tail, maxchars):
    lines = text.splitlines()
    if len(lines) > head + tail:
        omitted = len(lines) - head - tail
        lines = lines[:head] + [f"... [{omitted} lines elided]"] + lines[-tail:]
    text = "\n".join(lines)
    if len(text) > maxchars:
        text = text[:maxchars] + f"\n... [+{len(text) - maxchars} chars elided]"
    return text


def apply_A4(events):
    for e in events:
        if e["kind"] == "tool_result" and e["name"] == "Read":
            lines = e["text"].splitlines()
            kept = "\n".join(lines[:cfg.READ_HEAD_LINES])
            e["text"] = kept + f"\n[Read: {e['path']}, {len(lines)} lines total]"


def S1(events):
    for e in events:
        if (e["kind"] == "tool_result" and e["name"] != "Read"
                and not e["is_error"]):
            e["text"] = clip_lines(e["text"], cfg.S1_HEAD_LINES,
                                   cfg.S1_TAIL_LINES, cfg.S1_MAX_CHARS)


def S2(events):
    for e in events:
        if e["kind"] != "tool_use":
            continue
        inp = e["input"]
        for k in ("content", "old_string", "new_string"):
            v = inp.get(k)
            if isinstance(v, str) and len(v) > cfg.S2_FIELD_MAX_CHARS:
                inp[k] = (v[:cfg.S2_FIELD_MAX_CHARS]
                          + f" [+{len(v) - cfg.S2_FIELD_MAX_CHARS} chars]")


def S3(events):
    for e in events:
        if e["kind"] != "tool_result":
            continue
        if e["name"] == "Bash":
            e["text"] = clip_lines(e["text"], cfg.S3_BASH_HEAD,
                                   cfg.S3_BASH_TAIL, cfg.S1_MAX_CHARS)
        elif e["name"] in ("Grep", "Glob"):
            lines = e["text"].splitlines()
            if len(lines) > cfg.S3_GREP_FIRST:
                e["text"] = ("\n".join(lines[:cfg.S3_GREP_FIRST])
                             + f"\n... [{len(lines)} total lines]")


def S4(events):
    for e in events:
        if e["kind"] == "tool_result" and e["name"] != "Read":
            e["text"] = clip_lines(e["text"], cfg.S4_HEAD_LINES,
                                   cfg.S4_TAIL_LINES, cfg.S4_MAX_CHARS)


def S5(events):
    for e in events:
        if e["kind"] == "thinking" and len(e["text"]) > cfg.S5_THINKING_MAX_CHARS:
            e["text"] = (e["text"][:cfg.S5_THINKING_MAX_CHARS]
                         + f" [+{len(e['text']) - cfg.S5_THINKING_MAX_CHARS} "
                           "chars elided]")


def S6(events):
    for e in events:
        if e["kind"] == "thinking":
            e["text"] = f"[thinking: {e['orig_len']} chars elided]"


# --- render --------------------------------------------------------------
def render(events, header):
    out = ["```json", json.dumps(header, ensure_ascii=False, indent=2), "```", ""]
    for e in events:
        k = e["kind"]
        if k == "human":
            out += [f"## 🙋 Human — turn {e['n']}", "", e["text"], ""]
        elif k == "ai_text":
            out += ["## 🤖 AI", "", e["text"], ""]
        elif k == "thinking":
            out += ["### 💭 thinking", "", e["text"], ""]
        elif k == "tool_use":
            inp = json.dumps(e["input"], ensure_ascii=False)
            out += [f"### 🔧 {e['name']}", "", f"`input:` {inp}", ""]
        elif k == "tool_result":
            tag = " (error)" if e["is_error"] else ""
            out += [f"### ↳ {e['name']} result{tag}", "```",
                    e["text"], "```", ""]
    return "\n".join(out)


def main():
    args = sys.argv[1:]
    session = jsonl = None
    for i, a in enumerate(args):
        if a == "--session" and i + 1 < len(args):
            session = args[i + 1]
        if a == "--jsonl" and i + 1 < len(args):
            jsonl = args[i + 1]

    path = find_session_jsonl(session, jsonl)
    events = load_events(path)
    apply_A4(events)
    original = est_tokens(events)

    stages = [("S1", S1), ("S2", S2), ("S3", S3),
              ("S4", S4), ("S5", S5), ("S6", S6)]
    applied = []
    if est_tokens(events) > cfg.TARGET_TOKENS:
        for name, fn in stages:
            fn(events)
            applied.append(name)
            if est_tokens(events) <= cfg.TARGET_TOKENS:
                break
    reduced = est_tokens(events)

    header = {
        "source_jsonl": path,
        "original_tokens_est": original,
        "reduced_tokens_est": reduced,
        "target_tokens": cfg.TARGET_TOKENS,
        "structural_cleanup": [
            "dropped: file-history / isMeta / isCompactSummary / isSidechain",
            f"Read tool_result bodies kept to first {cfg.READ_HEAD_LINES} lines",
        ],
        "stages_applied": applied,
        "core_exceeds_target": reduced > cfg.TARGET_TOKENS,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    transcript_path = os.path.join(OUT_DIR, "transcript.md")
    with open(transcript_path, "w", encoding="utf-8") as fh:
        fh.write(render(events, header))

    tmpl = open(os.path.join(SKILL_DIR, "evaluator-prompt.md"),
                encoding="utf-8").read()
    prompt = (tmpl.replace("{{TRANSCRIPT_PATH}}", transcript_path)
                  .replace("{{PROJECT_ROOT}}", CWD)
                  .replace("{{READ_HEAD_LINES}}", str(cfg.READ_HEAD_LINES)))
    prompt_path = os.path.join(OUT_DIR, "prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)

    print(json.dumps({"transcript": transcript_path, "prompt": prompt_path,
                       "stats": header}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
