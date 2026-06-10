#!/usr/bin/env python3
"""merge-gate produce trigger — git `post-commit` hook (claude-harness-work #33).

Replaces the cwd-bound Stop "cheap scheduler" (`merge_gate_scheduler.py`) + the
PostToolUse dirty-mark (`merge_gate_mark.py`), both retired by ADR-0014. Those
self-gated on `is_local_profile(repo_root(cwd))` — a SESSION-scoped notion — so
under the established two-repo workflow (Claude session cwd in the plan repo
while editing the code repo) they no-op'd every turn and `produce` never fired,
leaving #31 with zero measurement data (the venue root cause).

A git `post-commit` hook is REPO-scoped: it fires in whatever repo the commit
lands in, independent of the Claude session's cwd. When the just-made commit
touched in-scope files it launches a backgrounded, COMMIT-PINNED
`merge-gate-local produce --coalesce` against HEAD — so the next push verifies a
fresh artefact for the committed tip instead of `missing` (the working-tree/
committed-tip timing bug is gone because both produce and verify hash the
committed range). It records the pending tuple {base, diff_hash, tip, pid} for
the verify hand-off (G2/G3) and redirects the producer's stdout to the state
dir's produce.log so the ⓑ auto-produce volume count keeps working (G5).

Never blocks the commit (always exits 0). Self-guards against recursion: a
no-op if a produce/validator child is itself committing (MERGE_GATE_PRODUCER_RUNNING).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

def resolve_import_roots(here, home):
    """(SCRIPTS, HOOKS) for importing the helper's deps. Prefer the helper's OWN
    checkout (`here` = the hooks/ dir; scripts/ is its sibling) so a checkout
    outside ~/.claude exercises ITS produce code AND assets — claude-harness-work
    #36 (import closure) + #37 (producer assets). #36's finding-1 pinned which
    helper FILE runs but not where its deps come from; #36 then pinned the .py
    import closure; #37 unifies the gate over the COMPLETE runtime set.

    UNIFIED gate (#37): pin to the checkout only when the COMPLETE runtime set is
    co-located — BOTH .py modules (merge_gate_local in scripts/, merge_gate_scheduler
    in hooks/=here) AND BOTH producer assets (the adversarial-review prompt + the
    review-output schema). This is the SAME predicate merge_gate_local.resolve_claude_dir
    uses for assets. Gating on a SUBSET here (e.g. #36's original two-.py gate)
    would let a checkout with the .py closure but missing assets PIN imports while
    merge_gate_local FALLS BACK its assets to $HOME — empty $HOME → the silent
    produce-trigger stop reading a non-existent prompt (ADR-0014); populated $HOME
    → review-asset version skew (f60c84a claude:finding-0). A partial checkout
    falls back wholesale, so imports and assets never decouple. `here` is already
    `Path(__file__).resolve()`-based, matching resolve_claude_dir's `.resolve()`.
    In a real $HOME/.claude install all four resolve under $HOME → byte-identical.

    The relpath list is DUPLICATED from merge_gate_local.RUNTIME_SET_RELPATHS (this
    hook must decide which merge_gate_local to import BEFORE it can import that
    constant); test_unified_gate_predicates_agree pins the two equal."""
    checkout = here.parent                                # <root>/hooks → <root>
    runtime_set = (
        checkout / "scripts" / "merge_gate_local.py",
        here / "merge_gate_scheduler.py",
        checkout / "scripts" / "merge-gate-assets" / "adversarial-review.md",
        checkout / "skills" / "setup-merge-gate" / "templates" / "review-output.schema.json",
    )
    if all(p.is_file() for p in runtime_set):
        return checkout / "scripts", here
    return home / ".claude" / "scripts", home / ".claude" / "hooks"


SCRIPTS, HOOKS = resolve_import_roots(Path(__file__).resolve().parent, Path.home())
WRAPPER = SCRIPTS / "merge_gate_local.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HOOKS))

try:
    import merge_gate_local as mg
except Exception:
    mg = None

# Reuse the retired scheduler's per-repo state helpers (ADR-0014: the helpers
# stay imported even though the Stop trigger is gone). The producer state dir
# (produce.log + state.json) is unchanged — ⓑ reads it exactly as before.
from merge_gate_scheduler import repo_state_dir, load_state, save_state, STATE_ROOT  # noqa: E402,F401


def launch_produce(root: Path, sdir: Path):
    """Fire-and-forget a backgrounded, commit-pinned, coalescing produce. The
    producer runs `merge-gate-local` DIRECTLY (it bypasses the measurement
    wrapper, exactly as the old scheduler did — design §2). Its stdout/stderr go
    to <state_dir>/produce.log so the `merge-gate produce: verdict=` line ⓑ counts
    is captured (G5). The child does NOT carry MERGE_GATE_PRODUCER_RUNNING — IT is
    the producer; the guard is set only on the validator/reviewer grandchildren.
    Returns the Popen (its .pid seeds the pending tuple), or None on failure."""
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        lf = open(sdir / "produce.log", "ab")
        return subprocess.Popen(
            [sys.executable, str(WRAPPER), "--cwd", str(root), "produce", "--coalesce"],
            cwd=str(root),
            env={k: v for k, v in os.environ.items()
                 if k != "MERGE_GATE_PRODUCER_RUNNING"},
            stdout=lf, stderr=lf, start_new_session=True,
        )
    except Exception:
        return None


def main():
    if mg is None:
        sys.exit(0)
    # Recursion guard: if a produce/validator child is itself committing, do NOT
    # trigger another produce (the #24/#26/#28/#29 runaway class; re-anchored from
    # the retired Stop scheduler to this hook — N4).
    if os.environ.get("MERGE_GATE_PRODUCER_RUNNING") == "1":
        sys.exit(0)
    # Repo-scoped: the repo the commit landed in is cwd. NO is_local_profile(cwd)
    # gate — being installed in this repo's .git/hooks IS the opt-in, and gating on
    # the session cwd was the venue bug ADR-0014 fixes.
    root = mg.repo_root(Path(os.getcwd()))
    if root is None:
        sys.exit(0)
    cfg = mg.load_config(root)
    base = mg.resolve_base_sha(root, cfg.base_ref)
    if base is None:
        sys.exit(0)
    tip = mg.rev_parse(root, "HEAD")
    if tip is None:
        sys.exit(0)
    # Commit-pinned: only the COMMITTED range counts (an out-of-scope or
    # ledger-only commit has an empty in-scope diff → produce nothing, consistent
    # with the no-row-on-ledger-push rule).
    cd = mg.canonical_diff_at_commit(root, base, tip, cfg.review_globs, cfg.ignore_globs)
    if cd.get("diff_error") or not cd["changed_files"]:
        sys.exit(0)
    sdir = repo_state_dir(root)
    proc = launch_produce(root, sdir)
    if proc is not None:
        # G3 — record the pending tuple at detach with the child's pid, so a push
        # that beats the producer's own first write still matches by tip_sha and
        # waits on a live producer. base/diff are known here (we computed them),
        # so there is no spin-up gap for the common case; the producer rewrites the
        # same tuple (and re-points it as it coalesces to a newer tip).
        mg.write_pending(root / cfg.artifact_root,
                         {"base_sha": base, "diff_hash": cd["diff_hash"],
                          "tip_sha": tip, "pid": proc.pid})
        # ⓑ: advance last_produce_ts (the produce.log `verdict=` count is the
        # primary signal; this keeps the since-prev delta's timestamp meaningful).
        try:
            st = load_state(sdir)
            st["last_produce_ts"] = time.time()
            save_state(sdir, st)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
