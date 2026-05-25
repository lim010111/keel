#!/usr/bin/env python3
"""Extract a recent conversational slice from the current Claude Code session
for embedding into a `consult-externals` council prompt.

Boundary policy (decided by council 3, 2026-05-25):
  - Primary marker: a Skill tool_use whose `input.skill` is one of the
    grilling skills (grill-me / grill-with-docs) on the active path.
  - Backward search limit: K human turns. If no marker is found within
    K turns, fall back to the last N human turns.
  - Slash-command-name and free-text matching ("grill", "grilling") are
    NOT used — they were empirically shown unreliable.

Output:
  - Writes the rendered slice to `.scratch/council/<slug>-excerpt.md`.
  - Prints a JSON stats blob to stdout.

Usage:
  prepare_slice.py <slug> [--session <id>] [--jsonl <path>]
"""
import glob
import json
import os
import re
import sys

# --- knobs (council 3 settled defaults; δ from council 4) ---
GRILLING_SKILLS       = {"grill-me", "grill-with-docs"}
MAX_BACKWARD_TURNS    = 15     # K — give up the marker search after this many human turns
FALLBACK_TURNS        = 5      # N — last-N-human-turns fallback when no marker
THINKING_MAX_CHARS    = 600    # mirrors third-party-review's S5_THINKING_MAX_CHARS
MAX_RENDERED_CHARS    = 40000  # δ — lazy overflow guard; common case (slice ≤ 40 KB) is untouched
OVERFLOW_AI_TEXT_MAX  = 500    # per-block AI-text cap once the guard re-renders for the second time

# Harness-emitted blocks in user-role messages — strip before deciding whether
# a user message carries real human text.
#   - <system-reminder>...</system-reminder>: inline harness reminders
#   - <task-notification>...</task-notification>: background-task completion events
#   - "Base directory for this skill: ...": the full skill-description payload
#       the harness injects as a user message whenever a slash-command/skill is
#       invoked. It is the SKILL.md content, not anything the user typed; the
#       human intent is already captured by the preceding <command-name> block.
SYSREMINDER       = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
TASK_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)
SKILL_INJECT      = re.compile(r"^Base directory for this skill:.*", re.DOTALL)


def strip_harness(text):
    text = SYSREMINDER.sub("", text)
    text = TASK_NOTIFICATION.sub("", text)
    text = SKILL_INJECT.sub("", text)
    return text


def find_session_jsonl(session=None, jsonl=None):
    """Locate the current session JSONL by cwd encoding.

    Resolution order:
      1. Explicit --jsonl path, if given.
      2. Explicit --session id, if given.
      3. $CLAUDE_CODE_SESSION_ID env var (set by Claude Code's CLI), which
         maps 1:1 to the JSONL basename.
      4. Newest mtime in the cwd's project dir (last resort).

    No cross-project fallback — if the cwd's project dir has no JSONL we
    fail loudly. The previous behaviour silently picked a JSONL from an
    unrelated project, producing wrong slices without any signal.
    """
    if jsonl:
        return jsonl
    if not session:
        session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    projects = os.path.expanduser("~/.claude/projects")
    enc = os.getcwd().replace("/", "-").replace(".", "-")
    proj_dir = os.path.join(projects, enc)
    candidates = glob.glob(os.path.join(proj_dir, "*.jsonl"))
    if not candidates:
        raise SystemExit(f"prepare_slice: no session JSONL in {proj_dir}")
    if session:
        for c in candidates:
            if os.path.basename(c).startswith(session):
                return c
        raise SystemExit(
            f"prepare_slice: session {session} not found in {proj_dir}")
    return max(candidates, key=os.path.getmtime)


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
    return raw


def active_path(raw):
    """Walk from the latest leafUuid backward through parentUuid.

    Returns (events_in_chronological_order, ok). ok=False when no reliable
    leaf is found — caller should fall back to the linear file order with
    a warning.
    """
    by_uuid = {o["uuid"]: o for o in raw if o.get("uuid")}
    leaf = None
    for o in raw:
        if o.get("type") == "last-prompt" and o.get("leafUuid"):
            leaf = o["leafUuid"]
    if not leaf or leaf not in by_uuid:
        return [], False
    chain, cur, seen = [], leaf, set()
    while cur and cur in by_uuid and cur not in seen:
        seen.add(cur)
        chain.append(by_uuid[cur])
        cur = by_uuid[cur].get("parentUuid")
    chain.reverse()
    return chain, True


def _grilling_skill(ev):
    """Return the grilling skill name (e.g. 'grill-me') if `ev` launches one,
    else None. Two recognized invocation paths:
      (a) assistant Skill tool_use with input.skill in GRILLING_SKILLS, OR
      (b) user-role text containing <command-name>/<skill></command-name>
          (the harness-injected marker for a DIRECT slash invocation).
    Empirically (b) is the common path: users almost always type `/grill-me`,
    which the harness records as a user-role message carrying the command-name
    tag — no assistant Skill tool_use is emitted. The earlier marker logic
    missed this case and silently fell back to the N-turn window.
    Compact-summary events are explicitly excluded so a harness-generated
    summary that happens to mention a grilling skill is never treated as
    the marker.
    """
    if ev.get("isCompactSummary"):
        return None
    m = ev.get("message")
    if not isinstance(m, dict):
        return None
    c = m.get("content")
    if isinstance(c, list):
        for b in c:
            if (isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and b.get("name") == "Skill"
                    and isinstance(b.get("input"), dict)
                    and b["input"].get("skill") in GRILLING_SKILLS):
                return b["input"]["skill"]
    if m.get("role") == "user":
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = "".join(b.get("text", "")
                           for b in c
                           if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        for sk in GRILLING_SKILLS:
            if f"<command-name>/{sk}</command-name>" in text:
                return sk
    return None


def is_grilling_marker(ev):
    """True iff `ev` launches a grilling skill (either invocation path)."""
    return _grilling_skill(ev) is not None


def is_human_turn(ev):
    """True iff `ev` carries real human text (not just tool_result / system-reminder).
    Compact-summary events are excluded so the harness-generated summary text
    is never counted as a human turn (it would otherwise inflate the K/N
    backward-search budgets and render as a fake `Human` block in the slice).
    """
    if ev.get("isCompactSummary"):
        return False
    m = ev.get("message")
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, str):
        c = [{"type": "text", "text": c}]
    if not isinstance(c, list):
        return False
    for b in c:
        if (isinstance(b, dict) and b.get("type") == "text"
                and strip_harness(b.get("text", "")).strip()):
            return True
    return False


def find_slice_start(events):
    """Return (start_index, mode, marker_skill).

    Walks backward from the leaf. If a grilling marker is found within
    MAX_BACKWARD_TURNS human turns, returns its index. Otherwise falls
    back to the index that opens the last FALLBACK_TURNS human turns.
    """
    n = len(events)
    if n == 0:
        return 0, "empty", None

    human_count = 0
    for i in range(n - 1, -1, -1):
        ev = events[i]
        skill = _grilling_skill(ev)
        if skill is not None:
            return i, "marker", skill
        if is_human_turn(ev):
            human_count += 1
            if human_count >= MAX_BACKWARD_TURNS:
                break

    # No marker found within K turns — last N human turns.
    human_count = 0
    for i in range(n - 1, -1, -1):
        if is_human_turn(events[i]):
            human_count += 1
            if human_count >= FALLBACK_TURNS:
                return i, "fallback", None
    return 0, "fallback", None  # less than N turns total: include everything


def render(events, mode, marker_skill, *, drop_thinking=False, ai_text_max=None):
    """Render the slice as markdown.

    Includes human turns, AI text, AI thinking (truncated), and Skill
    tool_use transitions. Other tool_use/tool_result are intentionally
    omitted — the slice is about what was *said*, not what was *done*.

    `drop_thinking` and `ai_text_max` are set only by the lazy overflow
    guard in `main()`; human turns are never affected.
    """
    out = ["<!-- Recent session excerpt — extracted from session JSONL active path"]
    if mode == "marker":
        out.append(
            f"     boundary: backward scan found Skill tool_use of "
            f"'{marker_skill}' as the grilling-start marker"
        )
    elif mode == "fallback":
        out.append(
            f"     boundary: no grilling-start marker found within "
            f"{MAX_BACKWARD_TURNS} human turns; using the last "
            f"{FALLBACK_TURNS} human turns as fallback"
        )
    else:
        out.append(f"     boundary: empty slice ({mode})")
    out.append(
        f"     thinking blocks truncated to {THINKING_MAX_CHARS} chars; "
        f"non-Skill tool_use and all tool_result omitted"
    )
    out.append("-->")
    out.append("")

    human_n = 0
    for ev in events:
        # Skip compaction-summary events: their string content is a harness-
        # generated summary of pre-compaction conversation, not anything the
        # human or the assistant said in turn. Rendering it would emit a fake
        # `## 🙋 Human` block carrying the harness's own paraphrase as
        # evidence — exactly the "agent edits its own evidence" failure mode
        # the slice exists to prevent.
        if ev.get("isCompactSummary"):
            continue
        m = ev.get("message")
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            c = [{"type": "text", "text": c}]
        if not isinstance(c, list):
            continue

        if role == "user":
            parts = []
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    txt = strip_harness(b.get("text", "")).strip()
                    if txt:
                        parts.append(txt)
            if parts:
                human_n += 1
                out += [f"## 🙋 Human — turn {human_n}", "",
                        "\n\n".join(parts), ""]
        elif role == "assistant":
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    t = b.get("text", "")
                    if ai_text_max is not None and len(t) > ai_text_max:
                        t = (t[:ai_text_max]
                             + f" [+{len(t) - ai_text_max} chars elided by overflow guard]")
                    if t.strip():
                        out += ["## 🤖 AI", "", t, ""]
                elif bt == "thinking":
                    if drop_thinking:
                        continue
                    th = b.get("thinking", "")
                    if len(th) > THINKING_MAX_CHARS:
                        th = (th[:THINKING_MAX_CHARS]
                              + f" [+{len(th) - THINKING_MAX_CHARS} chars elided]")
                    if th.strip():
                        out += ["### 💭 thinking", "", th, ""]
                elif bt == "tool_use" and b.get("name") == "Skill":
                    sk = (b.get("input") or {}).get("skill", "?")
                    out += [f"### 🔧 Skill: `{sk}`", ""]
    return "\n".join(out)


def _prepend_overflow_warning(rendered, action):
    """Insert a visible warning banner just after the HTML-comment header.

    The banner is plain markdown (a blockquote) so external advisors see it
    even though they ignore the comment block above.
    """
    warning = (
        f"> ⚠️ **Overflow guard applied** — rendered slice exceeded "
        f"{MAX_RENDERED_CHARS} chars; {action}. "
        f"Human turns are never truncated.\n\n"
    )
    marker = "-->\n"
    if marker in rendered:
        head, body = rendered.split(marker, 1)
        return head + marker + "\n" + warning + body
    return warning + rendered


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(
            "usage: prepare_slice.py <slug> [--session <id>] [--jsonl <path>]")
    slug = args[0]
    session = jsonl = None
    i = 1
    while i < len(args):
        if args[i] == "--session" and i + 1 < len(args):
            session, i = args[i + 1], i + 2
        elif args[i] == "--jsonl" and i + 1 < len(args):
            jsonl, i = args[i + 1], i + 2
        else:
            i += 1

    path = find_session_jsonl(session, jsonl)
    raw = load_events(path)
    active, ok = active_path(raw)
    if not ok:
        raise SystemExit(
            "prepare_slice: could not reconstruct active path "
            "(no last-prompt leafUuid in JSONL)")

    start, mode, marker_skill = find_slice_start(active)
    sliced = active[start:]
    rendered = render(sliced, mode, marker_skill)

    # Lazy overflow guard (council 4): the common case (slice ≤ MAX_RENDERED_CHARS)
    # is unaffected. Beyond the threshold, drop AI thinking first; if still over,
    # additionally truncate per-block AI text. Human turns are never touched.
    overflow_action = None
    if len(rendered) > MAX_RENDERED_CHARS:
        rendered = render(sliced, mode, marker_skill, drop_thinking=True)
        overflow_action = "dropped AI thinking blocks"
        if len(rendered) > MAX_RENDERED_CHARS:
            rendered = render(
                sliced, mode, marker_skill,
                drop_thinking=True, ai_text_max=OVERFLOW_AI_TEXT_MAX,
            )
            overflow_action = (
                f"dropped AI thinking; truncated each AI-text block to "
                f"{OVERFLOW_AI_TEXT_MAX} chars"
            )
        rendered = _prepend_overflow_warning(rendered, overflow_action)

    out_dir = os.path.join(os.getcwd(), ".scratch", "council")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{slug}-excerpt.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(rendered)

    print(json.dumps({
        "excerpt":              out_path,
        "source_jsonl":         path,
        "active_path_length":   len(active),
        "slice_start_index":    start,
        "slice_length":         len(sliced),
        "boundary_mode":        mode,
        "marker_skill":         marker_skill,
        "rendered_chars":       len(rendered),
        "overflow_action":      overflow_action,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
