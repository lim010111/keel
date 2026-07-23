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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
# The reduction pipeline + knobs live in the shared ~/.claude/scripts module
# (one owner with catch-up's prepare_lesson; same bootstrap path auto_fill
# uses to reach toml_sections).
_CLAUDE_SCRIPTS = os.path.abspath(os.path.join(SKILL_DIR, "..", "..", "scripts"))
if _CLAUDE_SCRIPTS not in sys.path:
    sys.path.insert(0, _CLAUDE_SCRIPTS)
import session_reduce as sr  # noqa: E402

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


# --- reduction pipeline: session_reduce owns it (A1-A4, S1-S6, render) ---


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

    # transcript modes (default or additive) — session_reduce owns the pipeline.
    path = find_session_jsonl(session, jsonl)
    events, rewound, path_ok = sr.load_events(sr.read_raw(path))
    original, reduced, applied = sr.reduce_events(events)

    cleanup = [
        "dropped: file-history / isMeta / isCompactSummary / isSidechain",
        f"Read tool_result bodies kept to first {sr.READ_HEAD_LINES} lines",
    ]
    if path_ok:
        cleanup.append(
            "rewound branches dropped — transcript is the active "
            "conversation path only (leafUuid -> root)")

    header = {
        "source_jsonl": path,
        "original_tokens_est": original,
        "reduced_tokens_est": reduced,
        "target_tokens": sr.TARGET_TOKENS,
        "structural_cleanup": cleanup,
        "stages_applied": applied,
        "core_exceeds_target": reduced > sr.TARGET_TOKENS,
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
        fh.write(sr.render(events, header))

    prompt_path = write_prompt(assemble("evaluator-transcript.md", {
        "{{TRANSCRIPT_PATH}}": transcript_path,
        "{{PROJECT_ROOT}}": CWD,
        "{{READ_HEAD_LINES}}": str(sr.READ_HEAD_LINES),
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
