#!/usr/bin/env python3
"""Narrative staleness guard — SessionStart snapshot + blocking Stop hook.

claude-harness-work status-harness#01. When this lands in claude-config,
reference it in commits as `refs status-harness#01`.

STATUS.md has two halves: a mechanical issue table that ~/.claude/scripts/
status.py regenerates every Stop (so it never drifts), and a hand-authored
NARRATIVE block (Current focus / Start here / Open decisions) that a hook
cannot author. The narrative goes stale because agents forget to run /status
after changing the project's posture. This hook builds the *enforcement* half:
it refuses to end a turn when posture changed this session but the narrative
was left byte-unchanged, re-prompting the agent to run /status. Automation as
enforcement, not authorship — modelled on tdd_verify.py.

Four argv subcommands:
  snapshot  — SessionStart: anchor {posture fingerprint, narrative hash,
              completion-offender baseline}.
  check     — Stop: block (exit 2) iff posture changed since the snapshot AND
              the narrative is byte-unchanged since the snapshot AND the
              session's transcript shows THIS session wrote at least one of
              the changed files (status-harness#04: the fingerprint reads
              repo-GLOBAL disk state while anchors are per (session, repo),
              so without attribution every concurrent session's change landed
              on whoever stopped first). The block names ONLY the attributed
              items. Also blocks when the narrative DID change but carries
              completion-labelled track lines (`- **완료 …**`) absent from the
              anchor's baseline — done tracks are deleted, not relabelled, and
              you clean what YOU wrote (legacy lines stay the advisory
              banner's job); detector shared with status.py (status-harness#03).
              A narrative change discharges the obligation only when STATUS.md
              itself is in the session's written-set; someone else's narrative
              edit is re-baselined (new reality accepted) WITHOUT exempting
              this session's own posture obligations.
  pause     — drop a (session, repo) marker that makes `check` exit 0 without
              blocking. A grilling skill (grill-with-docs / harden-issue) runs
              this up front: it edits ADRs / CONTEXT / issue files inline as
              decisions crystallise (real posture movement), but the narrative is
              refreshed only once grilling concludes — so a per-turn block mid-
              grilling is a false positive that pollutes the session. An env-var
              bypass can't carry this (hooks run from Claude Code's base env, not
              the agent's transient shell), so the signal is a file on disk.
  resume    — remove that marker (re-arm `check`). The skill runs this at the end,
              after /status has refreshed the narrative.

It reuses status.py's parse helpers (status.py stays a pure exit-0 regenerator;
this guard never modifies it).

Known gaps (by design):
  * Scope is Claude Code Stop only — the hook fires for Claude Code sessions.
    Cross-agent editing (Codex / antigravity touching this repo) is a known gap;
    git-native enforcement is deferred until cross-agent editing is real.
  * A pure issue-body change with no lifecycle transition, no ADR change, and no
    new Resolution block does NOT fire. That is the price of honouring principle
    P1 (an AC count bump is not a posture change): the fingerprint tracks
    lifecycle *state*, not prose. This uncovered case is rare and left to the
    next-session reader; the real incident (commit 5495b88) is covered because
    it created docs/adr/0013.
  * Attribution (status-harness#04) reads only write-tool tool_use entries and
    trackedFileBackups keys from the Stop transcript. Bash-mediated writes
    (`echo >`, `sed -i`, `git apply`), subagent edits (their tool_use lives in
    the subagent's own transcript), and issue deletions (`rm`) leave no record
    there: such posture moves are unattributable, so the guard stays silent —
    fail-open, "귀속 불명이면 관여하지 않는다". A staleness miss there is
    backstopped by the next session's staleness banner. Same for the lint: a
    completion line ADDED via Bash looks like someone else's narrative edit,
    so the foreign-change path absorbs it into the offender baseline without a
    lint-block (0eaca72 review, claude medium upheld) — the standing
    completion banner (status.completion_warning, regenerated every Stop) is
    the backstop that keeps flagging it. The cross-session FALSE
    attribution gap (an unrelated session drafted into narrating, the writing
    session silently discharged — the 2026-06-12 merge-gate#48 incident) is
    CLOSED by this attribution; cf. the known-gap table in status-harness#01's
    spec, which documents only the cross-agent (miss) direction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path.home() / ".claude" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import status  # noqa: E402  — pure parse helpers; status.main() is __main__-guarded.


def state_dir() -> Path:
    return Path(os.environ.get(
        "NARRATIVE_GUARD_STATE_ROOT",
        str(Path.home() / ".claude" / "hooks" / ".narrative-guard-state")))


def _repo_hash(root) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:12]


def snap_path(sid: str, root) -> Path:
    # Keyed by (session, repo root): one Claude Code session can span a checkout
    # and its worktree, or two issue repos (the plan-repo ↔ claude-config dance) —
    # cwd varies per hook event. A session-only key would cross-compare or clobber
    # anchors across repos (review #1). Each (session, repo) pair gets its own.
    return state_dir() / f"snapshot-{sid}-{_repo_hash(root)}.json"


def pause_marker_path(sid: str, root) -> Path:
    # Same (session, repo) keying as the snapshot: a pause set while grilling in
    # one repo must not silence the guard for a different repo in the same session,
    # and a forgotten `resume` self-heals next session (the sid changes).
    return state_dir() / f"pause-{sid}-{_repo_hash(root)}.json"


def read_input() -> dict:
    """Read hook JSON from stdin with a timeout guard against hangs."""
    def _timeout(_signum, _frame):
        raise TimeoutError

    data = ""
    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(5)
        data = sys.stdin.read()
        signal.alarm(0)
    except Exception:
        data = ""
    try:
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def repo_root(cwd) -> Path:
    """Project root for the *session's* cwd (the hook payload's cwd), not the
    hook process cwd. status.project_root() shells git against the process cwd,
    so it is deliberately not reused here (see status-harness#01 preflight)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except Exception:
        return Path(cwd)


# Column-0 anchor: a real Resolution block opens a line; prose mentions of the
# literal (table cells, indented `> **Resolution:**` in backticks — as in this
# very issue's body) are not column-0 and must not count (preflight footgun #2).
RESOLUTION_RE = re.compile(r"^> \*\*Resolution:\*\*", re.M)


def has_resolution(path: Path) -> bool:
    return bool(RESOLUTION_RE.search(path.read_text(encoding="utf-8")))


def has_markers(status_path: Path) -> bool:
    # Use the SAME shape status.read_narrative requires (START\n...\nEND), not a
    # loose substring check: otherwise markers it can't parse (e.g. both on one
    # line) would opt the project in while the narrative hash silently anchors
    # DEFAULT_NARRATIVE — an unclearable false block (review #8).
    if not status_path.exists():
        return False
    text = status_path.read_text(encoding="utf-8")
    pattern = (re.escape(status.NARRATIVE_START) + r"\n.*?\n"
               + re.escape(status.NARRATIVE_END))
    return bool(re.search(pattern, text, re.S))


def adr_fingerprint(root: Path) -> dict:
    """Map each docs/adr/*.md name to a sha256 of its content. Content-hash
    (not mtime) so a no-op turn never perturbs it, and an ADR *edit* — not just
    a new file — is caught. The real incident (commit 5495b88) added ADR-0013;
    a new file changes this map."""
    d = root / "docs" / "adr"
    if not d.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(d.glob("*.md")):
        # Per-file: one unreadable entry (broken symlink, dir-named .md, no read
        # perm) must not abort the whole fingerprint and silently disable the
        # guard (review #4). A sentinel still differs from a real hash, so the
        # entry's appearance/disappearance/repair is still detected.
        try:
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[p.name] = "<unreadable>"
    return out


def compute_fingerprint(root: Path) -> dict:
    """The posture fingerprint (A2): per-issue lifecycle state + Resolution-block
    presence, the issue/feature set, and the docs/adr/ fingerprint. Built by
    reusing status.py's parse helpers (A8). Every container is sorted so the
    JSON serialization is deterministic regardless of glob/dict/set ordering."""
    issue_files = sorted((root / ".scratch").glob("*/issues/*.md"))
    by_feature: dict[str, list[Path]] = {}
    for p in issue_files:
        by_feature.setdefault(p.parent.parent.name, []).append(p)

    lifecycle_map: dict[str, str] = {}
    resolution_map: dict[str, bool] = {}
    issue_set: list[str] = []
    for feature in sorted(by_feature):
        files = sorted(by_feature[feature])
        # Parse per-file: one undecodable/malformed issue must not abort the whole
        # fingerprint and silently disable the guard (review #6). A failed parse
        # gets a sentinel state, which still differs from a real lifecycle, so the
        # bad file's appearance/repair is detected — the guard stays live.
        parsed = []
        for p in files:
            try:
                parsed.append((p, status.parse_issue(p)))
            except Exception:
                parsed.append((p, None))
        by_num = {i["num"]: i for (_p, i) in parsed if i is not None}  # blocker scope
        for p, i in parsed:
            # Key by file stem, not issue number: two files sharing an NN
            # (a tracker-convention violation) stay distinct instead of one
            # silently shadowing the other in the dict (review #9).
            key = f"{feature}/{p.stem}"
            issue_set.append(key)
            if i is None:
                lifecycle_map[key] = "<unparseable>"
                resolution_map[key] = False
                continue
            lifecycle_map[key] = status.lifecycle(i, by_num)
            resolution_map[key] = has_resolution(p)

    return {
        "lifecycle": dict(sorted(lifecycle_map.items())),
        "resolution": dict(sorted(resolution_map.items())),
        "issues": sorted(issue_set),
        "features": sorted(by_feature.keys()),
        "adr": adr_fingerprint(root),
    }


def posture_changes(old: dict, new: dict) -> list[str]:
    """Human-readable lines naming what moved between two fingerprints (A6)."""
    changes: list[str] = []

    o_life, n_life = old.get("lifecycle", {}), new.get("lifecycle", {})
    for k in sorted(set(o_life) & set(n_life)):
        if o_life[k] != n_life[k]:
            changes.append(f"issue {k}: lifecycle {o_life[k]} → {n_life[k]}")

    o_iss, n_iss = set(old.get("issues", [])), set(new.get("issues", []))
    for k in sorted(n_iss - o_iss):
        changes.append(f"issue {k}: added")
    for k in sorted(o_iss - n_iss):
        changes.append(f"issue {k}: removed")

    o_res, n_res = old.get("resolution", {}), new.get("resolution", {})
    for k in sorted(set(o_res) & set(n_res)):
        if o_res[k] != n_res[k]:
            verb = "added" if n_res[k] else "removed"
            changes.append(f"issue {k}: Resolution block {verb}")

    o_adr, n_adr = old.get("adr", {}), new.get("adr", {})
    adr_names = sorted((set(o_adr) | set(n_adr)))
    moved = [n for n in adr_names if o_adr.get(n) != n_adr.get(n)]
    if moved:
        changes.append("ADR(s) changed: " + ", ".join(moved))

    return changes


# --- session attribution (status-harness#04) -------------------------------
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_WRITE_TOKENS = tuple(f'"{t}"' for t in WRITE_TOOLS)


def _norm(path) -> str:
    """Canonical form for written-set membership: realpath collapses symlinked
    prefixes so a tool-recorded path and a git-toplevel-derived path for the
    same file compare equal."""
    return os.path.realpath(str(path))


def written_set(payload: dict) -> set[str] | None:
    """Files THIS session wrote, scanned from the Stop payload's transcript (A1).

    Two sources, union'd: write-tool (Edit/Write/MultiEdit/NotebookEdit)
    tool_use inputs on `assistant` lines, and `trackedFileBackups` keys on
    `file-history-snapshot` lines (undocumented internal format — second
    source only, never relied on alone). Read/cat appear in neither, so a
    session that only READ a file can never be falsely attributed.

    Returns None when the transcript is absent or unreadable — the caller
    treats unknown attribution as "do not engage" (fail-open). Individually
    undecodable lines are skipped: a torn tail line (the file is being
    appended to right now) must not void the rest of the transcript, and a
    write entry lost that way only under-attributes, which fails open too.
    Lines are substring-prefiltered before json.loads so a p100 transcript
    stays ~100ms-class (measured 2026-06-12 over 1,100 transcripts: median
    62KB <1ms · p90 1.1MB 3-4ms · p100 36MB cold 99ms)."""
    tp = payload.get("transcript_path")
    if not tp:
        return None
    out: set[str] = set()
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "file-history-snapshot" not in line and not (
                        "tool_use" in line
                        and any(t in line for t in _WRITE_TOKENS)):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                kind = d.get("type")
                if kind == "assistant":
                    for b in (d.get("message") or {}).get("content") or []:
                        if (isinstance(b, dict) and b.get("type") == "tool_use"
                                and b.get("name") in WRITE_TOOLS):
                            inp = b.get("input") or {}
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            # Absolute entries only (0eaca72 review): a relative
                            # entry would realpath against the hook PROCESS cwd
                            # — typically this very project — and could FALSELY
                            # attribute someone else's change. Dropping it only
                            # under-attributes, which fails open.
                            if fp and os.path.isabs(fp):
                                out.add(_norm(fp))
                elif kind == "file-history-snapshot":
                    backups = (d.get("snapshot") or {}).get("trackedFileBackups") or {}
                    for k in backups:
                        if isinstance(k, str) and os.path.isabs(k):
                            out.add(_norm(k))
    except OSError:
        return None  # unreadable mid-scan: a partial set could misattribute
    return out


ADR_CHANGE_PREFIX = "ADR(s) changed: "
ISSUE_CHANGE_RE = re.compile(r"^issue ([^/]+)/(.+?): ")


def attribute_changes(changes: list[str], written: set[str], root: Path) -> list[str]:
    """Filter posture_changes() lines down to the ones THIS session wrote (A2):
    each line is reverse-mapped to its file (issue <feature>/<stem> ->
    .scratch/<feature>/issues/<stem>.md, ADR name -> docs/adr/<name>) and kept
    only when that file is in the written-set. The aggregate ADR line is
    re-rendered with only the attributed names — someone else's change must
    not even be MENTIONED (naming it invites the blocked session to narrate
    it; that change is the staleness banner's / the writing session's job).

    Granularity is the FILE, not the transition (0eaca72 review, codex medium
    upheld): a session that wrote the file is held to ANY posture move on it,
    including one a concurrent session made — disk state can't say who caused
    a transition without per-session content history. Accepted: such a session
    has real context on that file (the incident's harm was drafting a session
    with NONE), and the worst case is the pre-#04 status quo, a block."""
    out: list[str] = []
    for c in changes:
        if c.startswith(ADR_CHANGE_PREFIX):
            names = [n for n in c[len(ADR_CHANGE_PREFIX):].split(", ")
                     if _norm(root / "docs" / "adr" / n) in written]
            if names:
                out.append(ADR_CHANGE_PREFIX + ", ".join(names))
            continue
        m = ISSUE_CHANGE_RE.match(c)
        if m and _norm(root / ".scratch" / m.group(1) / "issues"
                       / f"{m.group(2)}.md") in written:
            out.append(c)
    return out


def block_message(changes: list[str]) -> str:
    detail = "\n".join(f"  • {c}" for c in changes)
    return (
        "Project posture changed this session but the STATUS.md narrative is "
        "unchanged. The narrative (Current focus / Start here / Open decisions) "
        "is judgement the status harness cannot author — update it so the next "
        "session starts oriented.\n\n"
        f"What changed:\n{detail}\n\n"
        "Run `/status` to refresh the narrative, then finish the turn. "
        "(Set NARRATIVE_GUARD_DISABLED=1 to bypass.)"
    )


def narrative_hash(status_path: Path) -> str:
    return hashlib.sha256(status.read_narrative(status_path).encode()).hexdigest()


def completion_block_message(offenders: list[str]) -> str:
    lines = "\n".join(f"  • {o}" for o in offenders)
    return (
        "Completed-track lines were written into the STATUS.md narrative this "
        "session:\n"
        f"{lines}\n\n"
        "Track labels are a closed set (능동/병행/휴면) — a finished track's "
        "line is DELETED, not relabelled. Where the content goes instead:\n"
        "  • forensics / what-happened detail → the issue's `> **Resolution:**` block\n"
        "  • a posture outcome → the Current-focus posture line / gate-table cell\n"
        "  • a live follow-up action → an 능동/병행 line naming that action\n\n"
        "Delete the line(s), then finish the turn. "
        "(Set NARRATIVE_GUARD_DISABLED=1 to bypass.)\n"
        "Rule: docs/agents/issue-tracker.md § The narrative is a status board, "
        "not a changelog."
    )


def disabled() -> bool:
    # Kill-switch (A7) + parity with status.py:248 — a merge-gate `produce` runs
    # a fresh `claude -p` whose SessionStart/Stop fire in this same repo; skip so
    # validator session ids never accrue junk snapshots or spuriously block.
    return (os.environ.get("NARRATIVE_GUARD_DISABLED") == "1"
            or os.environ.get("MERGE_GATE_PRODUCER_RUNNING") == "1")


def snapshot() -> None:
    if disabled():
        sys.exit(0)
    p = read_input()
    sid = str(p.get("session_id", "")) or "default"
    cwd = p.get("cwd") or os.getcwd()
    try:
        root = repo_root(cwd)
        # Write-once per (session, repo): a resume/compact re-fires SessionStart
        # with the same session_id; re-anchoring here would clobber a check-written
        # re-baseline (A5) and silently re-arm an already-acknowledged change.
        if snap_path(sid, root).exists():
            sys.exit(0)
        issue_files = sorted((root / ".scratch").glob("*/issues/*.md"))
        status_path = root / "STATUS.md"
        if not issue_files or not has_markers(status_path):
            sys.exit(0)  # opt-in: not a local-markdown issue project
        fp = compute_fingerprint(root)
        narr = status.read_narrative(status_path)
        state_dir().mkdir(parents=True, exist_ok=True)
        snap_path(sid, root).write_text(json.dumps({
            "fingerprint": fp,
            "narrative_hash": hashlib.sha256(narr.encode()).hexdigest(),
            # Offender baseline for the completion lint: `check` blocks only
            # lines absent from this anchor — you clean what you wrote, never
            # a legacy line someone else left (75a13c0 advisory review).
            "offenders": status.completion_offenders(narr),
        }))
    except Exception:
        pass  # fail-open: never raise on SessionStart
    sys.exit(0)


def check() -> None:
    if disabled():
        sys.exit(0)
    p = read_input()
    sid = str(p.get("session_id", "")) or "default"
    cwd = p.get("cwd") or os.getcwd()
    # Loop guard (mirror tdd_verify): this Stop was caused by our own block ->
    # enforce at most once per turn. Checked before the snapshot read so it never
    # re-baselines; an agent who ignores the block and re-stops is let through.
    if p.get("stop_hook_active") is True:
        sys.exit(0)
    try:
        root = repo_root(cwd)
        if pause_marker_path(sid, root).exists():
            # A grilling skill paused us for this (session, repo): the ADR / CONTEXT
            # / issue edits this turn are work-in-progress, not a settled posture
            # change. Never block; the skill runs /status + `resume` once grilling
            # concludes, which re-arms the guard against the real, final posture.
            sys.exit(0)
        sp = snap_path(sid, root)  # this session's anchor for THIS repo
        if not sp.exists():
            sys.exit(0)  # no anchor for this (session, repo) -> fail-open
        snap = json.loads(sp.read_text())
        if "fingerprint" not in snap or "narrative_hash" not in snap:
            sys.exit(0)  # corrupt snapshot -> fail-open
        issue_files = sorted((root / ".scratch").glob("*/issues/*.md"))
        status_path = root / "STATUS.md"
        if not issue_files or not has_markers(status_path):
            sys.exit(0)  # opt-in
        cur_fp = compute_fingerprint(root)
        cur_nh = narrative_hash(status_path)
    except Exception as exc:
        print(f"narrative_guard: skipped ({exc})", file=sys.stderr)
        sys.exit(0)  # fail-open: never block on infra error

    def rebaseline(fp, nh, off) -> None:
        try:
            sp.write_text(json.dumps({"fingerprint": fp,
                                      "narrative_hash": nh,
                                      "offenders": off}))
        except Exception:
            pass  # best-effort: never turn an allow into a block over a write error

    narrative_changed = (cur_nh != snap["narrative_hash"])
    changes = posture_changes(snap["fingerprint"], cur_fp)

    if narrative_changed:
        # Completion-label lint baseline (status-harness#03): you clean what
        # you wrote — only lines ABSENT from the anchor's offender baseline
        # count; a legacy offender survives even an unrelated narrative edit
        # (75a13c0 advisory review). An anchor predating the baseline key
        # cannot attribute lines, so it fails open.
        cur_off = status.completion_offenders(
            status.read_narrative(status_path))
        base = set(snap["offenders"]) if "offenders" in snap else None
        new_off = [o for o in cur_off if o not in base] if base is not None else []
        if not new_off and not changes:
            # Pure narrative refresh: nothing could block, so the transcript
            # is never read (A5 — a quiet Stop costs nothing extra).
            rebaseline(cur_fp, cur_nh, cur_off)
            sys.exit(0)
        written = written_set(p)
        if written is None:
            # Attribution unknown (no transcript_path / unreadable): do not
            # engage — keep the pre-#04 discharge behaviour (A4 fail-open).
            rebaseline(cur_fp, cur_nh, cur_off)
            sys.exit(0)
        if _norm(status_path) in written:
            # THIS session wrote the narrative. Lint first: a new offender
            # line blocks with NO re-baseline (the anchor stays until the
            # lines are gone). Otherwise the narrative edit discharges:
            # re-baseline everything so a *second* posture change later in
            # the same session is still caught (A5).
            if new_off:
                print(completion_block_message(new_off), file=sys.stderr)
                sys.exit(2)
            rebaseline(cur_fp, cur_nh, cur_off)
            sys.exit(0)
        # Someone ELSE moved the narrative (status-harness#04 incident): accept
        # the new reality for the hash + offender baseline — their prose, their
        # lint — but grant NO exemption. The fingerprint anchor is kept, so this
        # session's own posture obligations keep biting (a repeat block costs at
        # most one turn via stop_hook_active).
        rebaseline(snap["fingerprint"], cur_nh, cur_off)
        attributed = attribute_changes(changes, written, root)
        if attributed:
            print(block_message(attributed), file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    if changes:
        # Would-block path: posture moved, narrative untouched. Only NOW is the
        # transcript scanned (A5), and the block fires only for changes whose
        # files THIS session wrote (A2) — unknown attribution never engages
        # (A4). No re-baseline on the block path: the anchor stays until the
        # narrative actually moves.
        written = written_set(p)
        if written is None:
            sys.exit(0)
        attributed = attribute_changes(changes, written, root)
        if attributed:
            print(block_message(attributed), file=sys.stderr)
            sys.exit(2)
    sys.exit(0)


def _pause_identity() -> tuple[str, Path]:
    # pause/resume are invoked from the agent's Bash tool, not a hook, so there is
    # no stdin payload: read the session id from the env var Claude Code exports
    # (verified equal to the Stop hook's stdin session_id) and the repo from the
    # process cwd. Deliberately NOT reading stdin — that would hit read_input()'s
    # 5s timeout on every call.
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or "default"
    return sid, repo_root(os.getcwd())


def pause() -> None:
    """Mark the narrative guard paused for THIS (session, repo). See the module
    docstring: grilling edits posture inline but defers the narrative, so a per-
    turn block would be a false positive. Fail-soft — a pause that can't be
    written must never derail the grilling session."""
    sid, root = _pause_identity()
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        pause_marker_path(sid, root).write_text(
            json.dumps({"session": sid, "root": str(root)}))
    except Exception:
        pass
    sys.exit(0)


def resume() -> None:
    """Clear the pause marker for THIS (session, repo) and re-arm `check`. Run at
    the end of grilling, after /status has refreshed the narrative. Idempotent —
    a missing marker is not an error."""
    sid, root = _pause_identity()
    try:
        pause_marker_path(sid, root).unlink(missing_ok=True)
    except Exception:
        pass
    sys.exit(0)


def main() -> None:
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    if sub == "snapshot":
        snapshot()
    elif sub == "check":
        check()
    elif sub == "pause":
        pause()
    elif sub == "resume":
        resume()
    sys.exit(0)


if __name__ == "__main__":
    main()
