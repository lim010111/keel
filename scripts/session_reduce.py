#!/usr/bin/env python3
"""session_reduce — deterministic Claude Code session-transcript reduction.

The one owner of the pipeline two alignment-cluster skills consume:
third-party-review (prepare_review.py) and catch-up (prepare_lesson.py) used
to carry verbatim copies of it — already diverging, with the config validator
dropped on the catch-up side and zero test coverage on either. The copies are
gone; both skill scripts are thin CLIs over this module (they bootstrap
sys.path to ~/.claude/scripts, the same path auto_fill uses to reach
toml_sections).

Pipeline (priorities: human turns + AI text inviolable, thinking preserved
late, tool output cut first):
    read_raw(path)          JSONL -> raw entry list (tolerant of torn lines)
    load_events(raw)        A1-A3 structural cleanup + active-path
                            reconstruction -> (events, rewound, path_ok)
    apply_A4(events)        Read tool_result bodies -> head lines + stub
    reduce_events(events)   A4 + S1..S6 staged reduction until under budget
                            -> (original_est, reduced_est, stages_applied)
    render(events, header)  reduced markdown transcript

Reduction knobs live here (was third-party-review/reduction_config.py — tune
here only; _validate fails loudly at import on a broken knob). A consumer
with a different budget passes reduce_events(..., target_tokens=...).
"""
import json
import re

# --- reduction knobs ------------------------------------------------------
TARGET_TOKENS = 120_000   # 단계 축소의 유일한 멈춤 조건 (토큰)
CHARS_PER_TOKEN = 4       # 토큰 추정 제수 (chars / 4)

# A4: 항상 적용 — Read 도구 결과 본문에서 남길 줄수
READ_HEAD_LINES = 10

# S1: tool_result 본문 (Read·error 제외)
S1_HEAD_LINES, S1_TAIL_LINES, S1_MAX_CHARS = 30, 10, 2000
# S2: Write/NotebookEdit content, Edit old/new_string
S2_FIELD_MAX_CHARS = 400
# S3: Bash 결과 / Grep·Glob 결과
S3_BASH_HEAD, S3_BASH_TAIL, S3_GREP_FIRST = 20, 8, 5
# S4: tool_result 전체 (error 포함), 더 공격적으로
S4_HEAD_LINES, S4_TAIL_LINES, S4_MAX_CHARS = 8, 3, 600
# S5: thinking 블록 (S6 = thinking 통째 삭제, 파라미터 없음)
S5_THINKING_MAX_CHARS = 600


def _validate(g=None):
    g = dict(globals() if g is None else g)
    required = ["TARGET_TOKENS", "CHARS_PER_TOKEN", "READ_HEAD_LINES",
                "S1_HEAD_LINES", "S1_TAIL_LINES", "S1_MAX_CHARS",
                "S2_FIELD_MAX_CHARS", "S3_BASH_HEAD", "S3_BASH_TAIL",
                "S3_GREP_FIRST", "S4_HEAD_LINES", "S4_TAIL_LINES",
                "S4_MAX_CHARS", "S5_THINKING_MAX_CHARS"]
    for k in required:
        if k not in g:
            raise ValueError(f"session_reduce config: 키 누락 — {k}")
        if not isinstance(g[k], int) or g[k] <= 0:
            raise ValueError(
                f"session_reduce config: {k}는 양의 정수여야 함 (현재 {g[k]!r})")
    # 단조성: 후행 S4가 선행 S1보다 약하게 자르면 S4가 죽은 단계가 된다.
    if g["S4_HEAD_LINES"] > g["S1_HEAD_LINES"] or g["S4_MAX_CHARS"] > g["S1_MAX_CHARS"]:
        raise ValueError(
            "session_reduce config: S4_HEAD_LINES <= S1_HEAD_LINES 이고 "
            "S4_MAX_CHARS <= S1_MAX_CHARS 여야 함 — 아니면 S4 단계가 무의미.")


SYSREMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


# --- parse ----------------------------------------------------------------
def read_raw(path):
    """JSONL -> raw entry list; blank/torn lines are skipped, never fatal."""
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
    return raw


def is_human_turn(m):
    """True when a user message carries real human text — not just
    tool_result blocks or harness <system-reminder> noise."""
    content = m.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return False
    for b in content:
        if (isinstance(b, dict) and b.get("type") == "text"
                and SYSREMINDER.sub("", b.get("text", "")).strip()):
            return True
    return False


def active_path_uuids(raw):
    """Reconstruct the uuids on the active conversation branch.

    A Claude Code session JSONL is an append-only *tree*, not a linear log:
    when the human rewinds and resends, the abandoned branch stays in the
    file and a new branch is appended (anywhere — even at the file's end).
    The active conversation is the chain from the current leaf (the
    `leafUuid` on the last `last-prompt` entry) up through parentUuid to the
    root. Returns (uuid_set, ok); ok is False when no reliable leaf is found,
    and the caller then keeps the full linear transcript and warns.
    """
    by_uuid = {o["uuid"]: o for o in raw if o.get("uuid")}
    leaf = None
    for o in raw:                  # several last-prompt entries: last wins
        if o.get("type") == "last-prompt" and o.get("leafUuid"):
            leaf = o["leafUuid"]
    if not leaf or leaf not in by_uuid:
        return set(), False
    active, cur = set(), leaf
    while cur and cur in by_uuid and cur not in active:   # cur not in: cycle guard
        active.add(cur)
        cur = by_uuid[cur].get("parentUuid")
    # ok ONLY when the walk left the tree through an explicit root (no parent).
    # A still-set `cur` means it stopped early — the parent is missing (read_raw
    # deliberately skips torn JSONL lines, so this is a supported failure mode) or
    # the cycle guard tripped. Reporting True there made load_events treat a
    # PARTIAL uuid set as authoritative and discard every other message, silently
    # dropping earlier human turns; False keeps the linear transcript and warns.
    return active, not cur


def load_events(raw):
    """A1-A3 cleanup + active-path filter -> (events, rewound, path_ok)."""
    active, path_ok = active_path_uuids(raw)

    tool_names, tool_inputs = {}, {}
    for o in raw:
        m = o.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_names[b.get("id")] = b.get("name", "?")
                    tool_inputs[b.get("id")] = b.get("input", {})

    events, human_n, rewound = [], 0, 0
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
        # A3b — drop rewound branches: keep only messages on the active
        # path. An off-path user message carrying real text is a rewound
        # human turn; count it so the header can report the rewind.
        if path_ok and o.get("uuid") not in active:
            if m.get("role") == "user" and is_human_turn(m):
                rewound += 1
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
    return events, rewound, path_ok


# --- reduction ------------------------------------------------------------
def est_tokens(events):
    chars = 0
    for e in events:
        chars += len(e.get("text", ""))
        if e["kind"] == "tool_use":
            chars += len(json.dumps(e["input"], ensure_ascii=False))
    return chars // CHARS_PER_TOKEN


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
            kept = "\n".join(lines[:READ_HEAD_LINES])
            e["text"] = kept + f"\n[Read: {e['path']}, {len(lines)} lines total]"


def S1(events):
    for e in events:
        if (e["kind"] == "tool_result" and e["name"] != "Read"
                and not e["is_error"]):
            e["text"] = clip_lines(e["text"], S1_HEAD_LINES,
                                   S1_TAIL_LINES, S1_MAX_CHARS)


def S2(events):
    for e in events:
        if e["kind"] != "tool_use":
            continue
        inp = e["input"]
        for k in ("content", "old_string", "new_string"):
            v = inp.get(k)
            if isinstance(v, str) and len(v) > S2_FIELD_MAX_CHARS:
                inp[k] = (v[:S2_FIELD_MAX_CHARS]
                          + f" [+{len(v) - S2_FIELD_MAX_CHARS} chars]")


def S3(events):
    for e in events:
        if e["kind"] != "tool_result":
            continue
        if e["name"] == "Bash":
            e["text"] = clip_lines(e["text"], S3_BASH_HEAD,
                                   S3_BASH_TAIL, S1_MAX_CHARS)
        elif e["name"] in ("Grep", "Glob"):
            lines = e["text"].splitlines()
            if len(lines) > S3_GREP_FIRST:
                e["text"] = ("\n".join(lines[:S3_GREP_FIRST])
                             + f"\n... [{len(lines)} total lines]")


def S4(events):
    for e in events:
        if e["kind"] == "tool_result" and e["name"] != "Read":
            e["text"] = clip_lines(e["text"], S4_HEAD_LINES,
                                   S4_TAIL_LINES, S4_MAX_CHARS)


def S5(events):
    for e in events:
        if e["kind"] == "thinking" and len(e["text"]) > S5_THINKING_MAX_CHARS:
            e["text"] = (e["text"][:S5_THINKING_MAX_CHARS]
                         + f" [+{len(e['text']) - S5_THINKING_MAX_CHARS} "
                           "chars elided]")


def S6(events):
    for e in events:
        if e["kind"] == "thinking":
            e["text"] = f"[thinking: {e['orig_len']} chars elided]"


STAGES = [("S1", S1), ("S2", S2), ("S3", S3), ("S4", S4), ("S5", S5), ("S6", S6)]


def reduce_events(events, target_tokens=None):
    """A4 + staged S1..S6 until under budget.
    Returns (original_est, reduced_est, stages_applied)."""
    target = TARGET_TOKENS if target_tokens is None else target_tokens
    apply_A4(events)
    original = est_tokens(events)
    applied = []
    if est_tokens(events) > target:
        for name, fn in STAGES:
            fn(events)
            applied.append(name)
            if est_tokens(events) <= target:
                break
    return original, est_tokens(events), applied


# --- render ---------------------------------------------------------------
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


_validate()
