#!/usr/bin/env python3
"""Prepare third-party-review inputs.

Locates the current Claude Code session JSONL, reduces it deterministically
(structural cleanup A1-A4, then staged reduction S1-S6 only if over budget),
writes the reduced transcript and the assembled evaluator prompt into
<cwd>/.tpr/, and prints a JSON summary to stdout.

The review target defaults to the session transcript. Passing one or more
--target paths adds them as pinned evidence alongside the transcript (additive);
adding --only drops the transcript and reviews the targets alone.

Usage: prepare_review.py [--session <id>] [--jsonl <path>]
                         [--target <path> ...] [--only]
"""
import glob
import json
import os
import re
import shutil
import sys

SYSREMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SKILL_DIR)
import reduction_config as cfg  # noqa: E402

CWD = os.getcwd()
OUT_DIR = os.path.join(CWD, ".tpr")
TARGETS_DIR = os.path.join(OUT_DIR, "targets")
PER_FILE_WARN_BYTES = 200_000   # single target this big strains evaluator context
DIR_WARN_FILES = 50             # directory target this wide is likely unfocused


# --- locate session JSONL ------------------------------------------------
def find_session_jsonl(session=None, jsonl=None):
    if jsonl:
        return jsonl
    projects = os.path.expanduser("~/.claude/projects")
    enc = re.sub(r"[^A-Za-z0-9]", "-", CWD)  # CC maps every non-alnum char (incl _) to -
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


# --- active-path reconstruction (drop rewound branches) ------------------
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
    return active, True


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


# --- targets (copy into .tpr/targets/, build manifest) -------------------
def parse_args(argv):
    session = jsonl = None
    targets, only = [], False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--session" and i + 1 < len(argv):
            session = argv[i + 1]; i += 2; continue
        if a == "--jsonl" and i + 1 < len(argv):
            jsonl = argv[i + 1]; i += 2; continue
        if a == "--target" and i + 1 < len(argv):
            targets.append(argv[i + 1]); i += 2; continue
        if a == "--only":
            only = True
        i += 1
    return session, jsonl, targets, only


def copy_targets(targets):
    """Snapshot each target into .tpr/targets/ so all three evaluators read the
    same bytes from under cwd, regardless of the original location. Returns
    (entries, warnings, errors); each entry is {dest_rel, src, bytes} where
    dest_rel is relative to cwd (what the evaluator opens)."""
    entries, warnings, errors = [], [], []
    if os.path.isdir(TARGETS_DIR):
        shutil.rmtree(TARGETS_DIR)            # never review stale pins
    if not targets:
        return entries, warnings, errors
    os.makedirs(TARGETS_DIR, exist_ok=True)
    for idx, t in enumerate(targets, 1):
        src = os.path.abspath(os.path.expanduser(t))
        if not os.path.exists(src):
            errors.append(f"target not found: {t}")
            continue
        # containment: the REAL path (symlinks dereferenced) must be the project
        # root or strictly under it — these bytes are staged for EXTERNAL models.
        real = os.path.realpath(src)
        root = os.path.realpath(CWD)
        try:
            inside = os.path.commonpath([root, real]) == root
        except ValueError:
            inside = False
        if not inside:
            errors.append(f"{t}: resolves outside the project — refusing to copy "
                          "out-of-project bytes to external reviewers")
            continue
        base = os.path.basename(src.rstrip("/")) or "target"
        dest = os.path.join(TARGETS_DIR, f"{idx}-{base}")   # idx avoids collisions
        if os.path.isdir(src):
            # refuse copying a tree that contains our own output dir — e.g.
            # `--target .` would recursively pull .tpr (and the copy) into itself.
            if os.path.commonpath([os.path.abspath(OUT_DIR), src]) == src:
                errors.append(f"{t}: target contains the .tpr/ output dir — "
                              "refusing recursive copy")
                continue
            # a review tool feeds files to EXTERNAL models: never sweep in .git,
            # a nested .tpr, or symlinks (which can escape to secrets outside src).
            def _ignore(dirpath, names):
                return {n for n in names
                        if n in (".git", ".tpr")
                        or os.path.islink(os.path.join(dirpath, n))}
            shutil.copytree(src, dest, ignore=_ignore)
            files = [os.path.join(dp, f)
                     for dp, _, fs in os.walk(dest) for f in sorted(fs)]
            if len(files) > DIR_WARN_FILES:
                warnings.append(f"{t}: directory has {len(files)} files — "
                                "large target, evaluation may be unfocused")
            for f in files:
                entries.append({"dest_rel": os.path.relpath(f, CWD),
                                "src": os.path.relpath(f, dest) + f" (in {t})",
                                "bytes": os.path.getsize(f)})
        else:
            shutil.copy2(src, dest)
            n = os.path.getsize(dest)
            if n > PER_FILE_WARN_BYTES:
                warnings.append(f"{t}: {n} bytes — large target, may strain "
                                "evaluator context")
            entries.append({"dest_rel": os.path.relpath(dest, CWD),
                            "src": t, "bytes": n})
    return entries, warnings, errors


def manifest_text(entries):
    return "\n".join(f"- `{e['dest_rel']}` — from `{e['src']}` ({e['bytes']} bytes)"
                     for e in entries)


def targets_block(entries):
    if not entries:
        return ""
    return ("\nThe human has pinned these specific artifacts as high-signal "
            "evidence and copied them in full to `"
            + os.path.relpath(TARGETS_DIR, CWD)
            + "/` — they were elided from the reduced transcript, so read them in "
            "full and use them to judge the soundness of what the session "
            "produced:\n\n" + manifest_text(entries) + "\n")


def assemble(body_filename, subs):
    """common head + mode body, with {{...}} placeholders substituted."""
    common = open(os.path.join(SKILL_DIR, "evaluator-common.md"),
                  encoding="utf-8").read().rstrip("\n")
    text = open(os.path.join(SKILL_DIR, body_filename),
                encoding="utf-8").read().replace("{{COMMON}}", common)
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def write_prompt(text):
    prompt_path = os.path.join(OUT_DIR, "prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return prompt_path


# --- main ----------------------------------------------------------------
def main():
    session, jsonl, targets, only = parse_args(sys.argv[1:])
    os.makedirs(OUT_DIR, exist_ok=True)
    entries, warnings, errors = copy_targets(targets)

    # target-only: no transcript, no reduction, no session stats.
    if only:
        if not entries:
            raise SystemExit("prepare_review: --only needs at least one readable "
                             "--target — " + ("; ".join(errors) or "none given"))
        stale = os.path.join(OUT_DIR, "transcript.md")
        if os.path.exists(stale):
            os.remove(stale)                  # don't leave a transcript we didn't review
        prompt_path = write_prompt(assemble("evaluator-target.md", {
            "{{PROJECT_ROOT}}": CWD,
            "{{TARGETS_MANIFEST}}": manifest_text(entries),
        }))
        out = {"mode": "target-only", "prompt": prompt_path,
               "targets": [e["dest_rel"] for e in entries]}
        if warnings:
            out["warnings"] = warnings
        if errors:
            out["errors"] = errors
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # transcript modes (default or additive) — reduction pipeline unchanged.
    path = find_session_jsonl(session, jsonl)
    events, rewound, path_ok = load_events(path)
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

    cleanup = [
        "dropped: file-history / isMeta / isCompactSummary / isSidechain",
        f"Read tool_result bodies kept to first {cfg.READ_HEAD_LINES} lines",
    ]
    if path_ok:
        cleanup.append(
            "rewound branches dropped — transcript is the active "
            "conversation path only (leafUuid -> root)")

    header = {
        "source_jsonl": path,
        "original_tokens_est": original,
        "reduced_tokens_est": reduced,
        "target_tokens": cfg.TARGET_TOKENS,
        "structural_cleanup": cleanup,
        "stages_applied": applied,
        "core_exceeds_target": reduced > cfg.TARGET_TOKENS,
        "active_path_reconstructed": path_ok,
        "rewound_human_turns": rewound,
    }
    if not path_ok:
        header["warning"] = (
            "no leafUuid found — could not reconstruct the active path; "
            "transcript may include rewound (abandoned) branches that the "
            "main agent never actually had in context")

    transcript_path = os.path.join(OUT_DIR, "transcript.md")
    with open(transcript_path, "w", encoding="utf-8") as fh:
        fh.write(render(events, header))

    prompt_path = write_prompt(assemble("evaluator-transcript.md", {
        "{{TRANSCRIPT_PATH}}": transcript_path,
        "{{PROJECT_ROOT}}": CWD,
        "{{READ_HEAD_LINES}}": str(cfg.READ_HEAD_LINES),
        "{{TARGETS_BLOCK}}": targets_block(entries),
    }))

    out = {"mode": "additive" if targets else "transcript",
           "transcript": transcript_path, "prompt": prompt_path, "stats": header}
    if entries:
        out["targets"] = [e["dest_rel"] for e in entries]
    if warnings:
        out["warnings"] = warnings
    if errors:
        out["errors"] = errors
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
