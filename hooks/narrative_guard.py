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
  snapshot  — SessionStart: anchor {posture fingerprint, narrative hash}.
  check     — Stop: block (exit 2) iff posture changed since the snapshot AND
              the narrative is byte-unchanged since the snapshot.
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


def block_message(old: dict, new: dict) -> str:
    changes = posture_changes(old, new)
    detail = "\n".join(f"  • {c}" for c in changes) or "  • (posture fingerprint changed)"
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
        nh = narrative_hash(status_path)
        state_dir().mkdir(parents=True, exist_ok=True)
        snap_path(sid, root).write_text(
            json.dumps({"fingerprint": fp, "narrative_hash": nh}))
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

    narrative_changed = (cur_nh != snap["narrative_hash"])
    if narrative_changed:
        # The agent touched the narrative this session (did their job, or ran
        # /status). Re-baseline to {current fingerprint, current narrative} so a
        # *second* posture change later in the same session is still caught (A5).
        # Checked before posture so re-baseline beats block when both moved.
        try:
            sp.write_text(
                json.dumps({"fingerprint": cur_fp, "narrative_hash": cur_nh}))
        except Exception:
            pass  # best-effort: never turn an allow into a block over a write error
        sys.exit(0)

    posture_changed = (json.dumps(cur_fp, sort_keys=True)
                       != json.dumps(snap["fingerprint"], sort_keys=True))
    if posture_changed:
        # Do NOT re-baseline on the block path: leaving the original anchor in
        # place means the next Stop still sees the change until the narrative
        # actually moves.
        print(block_message(snap["fingerprint"], cur_fp), file=sys.stderr)
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
