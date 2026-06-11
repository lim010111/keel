---
name: harness-doctor
description: Diagnose a target repo's harness-scaffold conformance (which applicable gates/skills are installed vs missing, on the intent and enforcement axes), on a first run propose + record an intended `[harness]` profile in the repo's harness.toml, then auto-fill the gaps by delegating to each setup skill's own apply path (consent-gated, footgun-refusing, repo-scope by default). Runs the read-only `harness_doctor.py` engine + the `auto_fill.py` dispatcher. Use when the user asks to "check harness scaffold", "is this repo set up", "run harness-doctor", "/harness-doctor", "fill the gaps", "스캐폴드 점검", "이 레포 하네스 깔려있는지 봐줘", "빠진 거 채워줘". Read-only unless you confirm a record or a fill.
---

# Diagnose harness-scaffold conformance and record the intended profile

The doctor reports which of a **target repo**'s applicable harness scaffold is
installed, on two axes that are never collapsed (ADR-0020): **intent** (in-tree
config — portable, CI-assertable) vs **enforcement** (machine-local git hooks —
fail-open, gone on a fresh clone). Coverage is measured against a recorded
*intended* profile; this skill is how that profile is proposed and recorded.

The deterministic work lives in the read-only engine
`~/.claude/scripts/harness_doctor.py` (scaffold-doctor #02/#03). **The engine
never writes** — it diagnoses, proposes, and computes coverage. The single
`harness.toml` write is performed *here* (the skill), via
`scripts/record_profile.py`. That boundary is the load-bearing safety invariant
(AC15): never call a write path from the engine.

## Walkthrough — diagnose → record → fill (one flowing surface, #05)

Run the stages in order; **each stage is a valid stopping point**. A pure
diagnosis (stop after step 1) leaves the repo untouched; recording (step 4) is
the first write and happens only on operator confirmation; filling (steps 5–8)
is consent-gated per tier. Declining at any stage costs nothing.

1. **Diagnose (always, read-only).** Run the engine and show the operator the
   two-axis report:

   ```
   python3 ~/.claude/scripts/harness_doctor.py [<repo>]          # human table
   python3 ~/.claude/scripts/harness_doctor.py [<repo>] --json   # machine report
   ```

   Exit code is the #02 gap rule (0 clean / 1 not-a-repo / 2 gaps) — coverage
   never changes it. If a `[harness]` profile is already recorded, the report
   includes the **coverage** block; stop here unless the operator wants to
   re-record.

2. **Propose (first run only, read-only).** If, and only if, **no `[harness]`
   is recorded** (`harness_doctor.read_recorded_profile(repo)` is `None`) **and**
   the session is interactive, call `harness_doctor.propose_profile(repo)`. It
   returns a candidate `scaffold` + a `ci` recommendation from mechanical signals:

   | Signal (read-only) | Surfaces |
   |---|---|
   | any git repo | `agents-md` + `status-harness` (base — no judgment row) |
   | a git **remote** | `merge-gate` candidate (a remote ≠ review is *wanted*) |
   | a **build manifest** (`package.json`/`pyproject.toml`/`Cargo.toml`/`go.mod`) | the **CI** judgment row |
   | a **test suite** (`tests/`\|`test/` or a `test_*.py`/`*_test.go`/… file) | the dormant **self-verification** opt-in row |

3. **Interview — the irreducible judgments, one `AskUserQuestion` at a time.**
   `wanted ≠ applicable`: a detected signal only *surfaces* a row; the operator
   decides. Ask **only the rows the signals surfaced**, **one question per call**,
   and only on this first run:

   - **Want CI?** → records `ci = <bool>`. CI has no mechanical detector, so it is
     recorded as a judgment and reported "wanted / not-yet-measurable" — never a
     term in the coverage fraction (until #07 gives `ci-setup` a `--json` probe).
   - **Want merge review?** → decides whether `merge-gate` enters `scaffold`.
   - **Opt into the dormant `self-verification` concern** (so phase-2 can track
     it) **or leave it out?** → decides `self-verification` membership. This
     records **intent only** — it never activates the parked gate (no installer
     exists; activation stays #04's report-only refusal). Once recorded,
     self-verification is reported "opted-in, parked", excluded from the coverage
     denominator (never nagged).

   `agents-md` and `status-harness` are the base — no row, always in `scaffold`.

4. **Record (the one write).** Build the confirmed profile
   `{"scaffold": [...], "ci": <bool>}` and write it:

   ```python
   import record_profile        # ~/.claude/skills/harness-doctor/scripts/
   record_profile.record_profile(repo, {"scaffold": [...], "ci": bool})
   ```

   The write is a **section-scoped, comment-preserving text merge** (AC16): it
   appends/replaces only the `[harness]` block and leaves every sibling gate
   section and its inline comments byte-for-byte intact. It does **not**
   re-serialize the file (a lossy `tomllib`→writer rewrite is refused — the real
   target repos and `~/.claude/harness.toml` are richly annotated). `scaffold`
   slugs are EXACTLY the engine's probe IDs (`merge-gate`, **never**
   `merge-gate-local`) so coverage intersects cleanly.

## Headless / CI

"Headless" is signalled **explicitly**, never inferred from `--json` (AC18).
A non-interactive run (CI, a scripted invocation, or `--non-interactive`):

- runs the engine read-only and reports raw per-concern presence (or coverage,
  if a `[harness]` is already recorded), exiting per the #02 gap rule;
- **never** runs the interview and **never** calls `record_profile` — proposal is
  interactive-first-run-only. A repo with no `[harness]` is reported as-is (its
  absence is not itself a gap), not auto-proposed.

## Auto-fill (scaffold-doctor #04) — walkthrough steps 5–8

Once a `[harness]` profile is recorded, **fill the gaps** by delegating to each
setup skill's own apply path. The deterministic dispatch lives in the read-only-
to-plan / consented-to-apply module `scripts/auto_fill.py`; the engine still never
writes. The fill set is `{diagnose gaps} ∩ {recorded scaffold}` — **with no
`[harness]` recorded, fill is a no-op** that points back at the record step.

**The staged flow (no Python callable ever crosses this boundary):**

5. **Plan (read-only).** Build the JSON fill plan:
   ```
   python3 ~/.claude/skills/harness-doctor/scripts/auto_fill.py [<repo>] --json
   ```
   Each record is `{concern, scope (repo|global), consent_tier (auto|confirm|
   refuse), kind, message, action_id}`. Show the operator the `auto` (will apply),
   `confirm` (needs go-ahead), and `refuse`/`report` (surfaced, not auto-closed)
   groups. **A `refuse`/`report` is intentional, not a failure** — e.g. a diverged
   vendored file or a parked concern; say so, so a non-zero diagnose exit is not
   read as "fill failed".

6. **Apply the auto tier (repo scope).** Pure-create / additive work applies
   without a prompt:
   ```
   python3 ~/.claude/skills/harness-doctor/scripts/auto_fill.py [<repo>] --apply
   ```

7. **Confirm tier — one `AskUserQuestion` per concern.** For each `confirm` record
   (a `kind = "migrate"`, e.g. `CLAUDE.md → AGENTS.md`; or a merge-gate that will
   back up a foreign hook), get an explicit go-ahead, then apply only the
   confirmed ones:
   ```
   python3 ~/.claude/skills/harness-doctor/scripts/auto_fill.py [<repo>] --apply-confirmed <action_id> [<action_id> ...]
   ```
   Mention that a migrate is git-recoverable.

8. **Global-bootstrap — ONE consent for all global writes.** If the plan carries a
   `scope = "global"` record, the operator-global `~/.claude` layer is pending
   (status.py + SessionStart/Stop hooks and/or stale-hook cleanup). It is **never**
   part of the per-repo auto-apply. Ask **once** for the whole global step, then:
   ```
   python3 ~/.claude/skills/harness-doctor/scripts/auto_fill.py --global-bootstrap
   ```
   It detects pending work read-only first (a content-checked no-op when already in
   place) and returns `status: blocked` on an invalid `~/.claude/settings.json`
   rather than half-writing.

**Footgun refusals are automatic** (you do not need to reason about them): a
merge-gate whose `pre-push`/`post-commit` already carries our marker is
report-only (a re-render would clobber a prepended block — the #41 `#31`-RETIRED
tombstone is the live instance); an unrecognized `[merge-gate]` (anything but
profile `local` — the only profile, ADR-0021) is report-only (auto-fill never
installs onto a section it does not understand); an out-of-repo `core.hooksPath` is report-only
(two-scope leak). Re-running a full `install_local.py` is never how the gap is
filled — auto_fill calls the repo-scoped functions directly.

## Scope

This skill diagnoses (#02), records the intended profile (#03), auto-fills the
gaps (#04), and presents them as the single diagnosis→record→fill walkthrough
above (#05). The engine stays an internal module (`harness_doctor.py`) that CI
and phase-2 import directly — this skill is the **one** operator-facing surface,
a face over the engine, never a gatekeeper of it. Phase-2 (integrity/drift) is
#06; the installer `--json`/exit-code contract retrofit is #07.
