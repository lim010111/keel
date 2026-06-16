---
name: handle-merge-findings
description: Consumer-side handling of advisory merge-gate findings (#49, ADR-0027) — the implementing session's single-pass, human-gated reproduce-or-refute loop. Read all findings as-is (the validator verdict is a HINT, never a filter — #31 measured it 100% over-blocking) → triage by judgment → delegate reproduce-or-refute to a sub that returns a runnable FAILING test (an oracle), never a bare verdict → main re-runs it to confirm → delegate the fix to a SEPARATE sub that makes the frozen test pass without editing it → batch confirmed fixes into ONE commit+push → hand off (the human gates any pass 2+). Use after pushing in-scope changes when a merge-gate local profile is installed, when the user asks to "handle the findings", "run a findings pass", "reproduce or refute the merge-gate findings", invokes /handle-merge-findings, or for pass 2+ on a prior fix-push's new review. Korean: "머지 게이트 findings 처리", "findings 한 패스 돌려줘", "리뷰 지적 재현해서 처리".
---

# Handle advisory merge-gate findings — the consumer-side loop

The merge gate is **advisory end-state** (#31: at reliable N=15, 100% of blocking
findings were over-blocks, 0 confirmed true-positives — yet real bugs *do* surface,
#40). So findings are **rare signal buried under false positives**. This skill is
the implementing session's *empirical* handling of them (CONTEXT.md → *finding
reproduction*): after your change is pushed, you run **one automatic pass** that
proves or refutes each finding by running the code, fixes only the proven ones, and
hands off. The decisions here are locked in **ADR-0027** — do not re-litigate them.

**The gate is never touched** (instrument-around, ADR-0009; D1, ADR-0014): this
loop only *reads* findings and acts in the work interval. It changes no
`produce`/`verify`/`validator` verdict or exit code.

## Hard invariants (these ARE the design — ADR-0027)

- **Validator verdict is a HINT, never a filter.** Read every finding as-is; no
  severity gate, no validator-upheld filter. The verdict is the one signal #31
  measured unreliable. You judge content.
- **Reproduce sub returns an oracle, not an opinion.** Its deliverable is a
  *runnable test that fails on HEAD*, or a refutation — never a bare "it's real."
  You consult the oracle; you never trust the sub's verdict.
- **Prove before fix, absolutely.** Only findings you re-confirmed real (the
  oracle fails on HEAD in *your* hands) are auto-fixed. Unprovable-but-suspected
  findings are **surfaced to the human**, never fixed to appease the gate.
- **Separate reproduce sub and fix sub.** The refuter has no stake in a fix; you
  gate between proof and fix. This is the structural defense against the #1 risk —
  churning code to satisfy a ~always-false finding.
- **Fix sub may not weaken the oracle.** It makes the *frozen* failing test pass
  without editing it; you verify the test file is byte-unchanged afterward.
- **Single pass; the human gates re-entry.** Pass 1 runs automatically after the
  implementation push. After you batch-push the fixes, **exit** — the new review
  the fix-push triggers is the human's to read. Never auto-loop.

## When this runs (M1 trigger)

Two entries, same loop:
- **Pass 1 (automatic):** the AGENTS.md operating-protocol anchor (wired by
  `/setup-merge-gate`) says: *after pushing in-scope changes, run one findings
  pass, then hand off.* That is this skill.
- **Pass 2+ (human-invoked):** the human reads the fix-push's new findings and, if
  worth another round, invokes `/handle-merge-findings` again.

No async Stop/PostToolUse hook is involved — those no-op under the plan-repo /
code-repo two-repo split (memory `project_harness_hooks_cwd_venue`). The trigger is
*you* running the read step explicitly.

## Step 0 — No-op precondition

Inert unless the **merge-gate local profile** is installed in this repo. Check for
`[merge-gate]` in `harness.toml` (or a `merge-gate-local` pre-push hook). If absent,
**stop** — there are no findings to handle. The loop assumes the **advisory**
posture (the push already succeeded).

## Step 1 — Read the findings (waits for the in-flight produce)

The `post-commit` produce is async (~6–9 min). The read step **waits** for it (up
to `MERGE_GATE_VERIFY_WAIT_SECONDS`, default 900s), so run it with a **long Bash
timeout** — set the Bash tool `timeout` to **≥ 960000 ms** or the call is killed
mid-wait.

```
python3 ~/.claude/scripts/merge_gate_local.py findings --tip-sha HEAD --json
```

This is **read-only** (writes nothing — D1). It surfaces, for the pushed
`(base, tip)`, each finding's **reviewer content** (`title`/`body`/`recommendation`/
`reviewer_confidence`) joined with the gate's `severity`/`validator_verdict`/`block`
and the validator's `citation`. Parse the JSON.

- `state: "missing" | "no-changes"` with empty `findings` → nothing to handle.
  Note it and **exit** (this is the common, cheap path under 100% over-block).
- `state: "pending-timeout"` → a review **matched this push and is still in
  flight** (produce ran past `MERGE_GATE_VERIFY_WAIT_SECONDS`); `pending_tip` names
  it. This is **not** "no findings" — do **not** exit as nothing-to-handle. Re-run
  the read once after a pause, or surface "review still running" to the human, so an
  in-flight (possibly blocking) review is not silently dropped.
- `findings: [...]` → continue to triage.

(`findings-log.md` in the artefact root is only a thin index — location + verdict +
citation. The **content to judge** is what this command joins in from the
per-reviewer `findings.json`. Read the command's output, not the archive.)

## Step 2 — Triage by judgment (content-based, no severity gate)

For each finding, read its content and classify — the validator verdict is context,
not a decision:

- **Non-falsifiable** (style, docs, architecture, naming, "consider…") → resolve
  here by judgment. Apply a trivially-correct one if you agree; otherwise note and
  carry to the handoff. **Never send these to a reproduce sub.**
- **Falsifiable correctness claim worth checking** (a concrete defect: wrong
  result, crash, race, off-by-one, missing guard) → queue for reproduce-or-refute.
- **Clearly spurious** (contradicts the code you just wrote, or the change you made
  already handles it) → drop with a one-line reason in the handoff.

Keep your own context light: the heavy reproduction/fix work runs in subs (below).
You hold only triage + decisions + the subs' compact results.

## Step 3 — Reproduce-or-refute (delegated sub; one per queued finding)

For each falsifiable finding, dispatch a **`general-purpose`** sub-agent with
**`/diagnose` discipline**. It is adversarial — its default answer is *refuted*.

**Contract — the sub returns exactly this (and writes a test file):**
```json
{ "verdict": "real" | "refuted",
  "test_path": "<path to a runnable test that FAILS on HEAD>",
  "repro_cmd": "<exact command to run that test>",
  "note": "<one paragraph: what it proves, or why it's refuted>" }
```
- `verdict: "real"` is only valid **with** a `test_path` whose `repro_cmd` **fails
  on the current HEAD** — a runnable oracle, never a bare claim. If the sub cannot
  produce a failing test, the answer is `refuted` (or "unprovable" in the note).
- Give the sub the finding's full content (title/body/recommendation/file:line) and
  tell it: *prove the defect bites by writing a failing test, or refute it; do not
  fix anything.*

## Step 4 — Confirm the oracle yourself (prove-before-fix)

For each `real` verdict, **you** run `repro_cmd` and confirm the test **fails on
HEAD**. This is the gate between proof and fix:

- Fails as claimed → it's a **confirmed real**; proceed to fix.
- Does **not** fail (passes, errors out, or won't run) → **not proven**. Do **not**
  fix it. Carry it to the handoff as *suspected, unprovable* for the human.

## Step 5 — Fix (a SEPARATE delegated sub; confirmed reals only)

For each confirmed real, dispatch a **fresh `general-purpose`** sub with **`/tdd`
discipline**, handing it the **frozen** oracle (`test_path`):

**Contract:**
```json
{ "diff": "<source changes only>",
  "frozen_test_green": true,
  "oracle_unchanged": true,
  "note": "<what the fix does>" }
```
Tell it: *make this frozen failing test pass by fixing the source. You may NOT edit
`<test_path>` — weakening the oracle is forbidden.*

**Then verify yourself (oracle-weakening guard):**
1. `git diff -- <test_path>` shows **no change** to the oracle (byte-unchanged). If
   the sub touched it, reject the fix and re-dispatch.
2. Re-run `repro_cmd` — the oracle now **passes**.

## Step 6 — Batch, push once, hand off

- **Batch ALL confirmed fixes (+ their frozen oracles) into ONE commit**, then one
  `git push`. Do **not** make per-fix commits — each fires a `post-commit` produce.
  The oracle test lands *with* its fix (it's the regression test).
- **Exit with a handoff**, stating:
  - what was fixed (per finding: the oracle + the fix), batched into the one push;
  - what was **surfaced, not fixed** (non-falsifiable triage calls; suspected-but-
    unprovable findings);
  - that the fix-push triggered a **new** advisory review whose findings are **not**
    auto-handled — the human decides whether to run pass 2 (`/handle-merge-findings`).

Stop here. Do not start a pass 2 yourself.

## Notes

- Authority: **ADR-0027** (the six locked forks) · issue **#49** (spec + AC) ·
  ADR-0009 (instrument-around) · 0014 (post-commit / D1) · 0021 (advisory ceiling)
  · 0010 (subs are fresh, context-independent reviewers).
- Read interface: `merge_gate_local.py findings` (read-only subcommand; reuses the
  gate's produce-wait). Term: CONTEXT.md → *finding reproduction*.
- **Pre-push alternative (flagged, low-confidence):** `produce` fires at
  *post-commit*, so handling findings *between commit and push* would keep buggy
  intermediates off `main`. ADR-0027 chose **post-push**; revisit only if churn
  becomes a problem.
