---
name: matt-drift-watch
description: Advisory drift check of our rented Matt Pocock skills against upstream HEAD — reports what changed and whether it's worth reflecting; never syncs or commits.
disable-model-invocation: true
---

# Matt drift watch

We **rent** Matt Pocock's skills, we don't fork them (ADR-0031/0032): the base lives
unversioned in `~/.agents/skills/`, and drift against upstream is an **advisory watch**,
not a forced sync. This skill runs that watch — it *reports* what moved and whether it's
worth reflecting.

Two things make the judgment non-mechanical, so they live in [`manifest.md`](manifest.md):
our **protected deltas** (edits we made on purpose — clobbering them on a blind resync is
the main hazard) and our **standing decisions** (upstream skills we've already decided not
to adopt). Read it before you classify.

## Steps

1. **Scan.** Run `bash scan.sh`. It fetches upstream HEAD and prints, per rented skill,
   `IDENTICAL` or the raw `diff -r`, plus any upstream skill with no local match as `NEW`.
   In each `diff -r`, `<` is our rented copy and `>` is upstream HEAD (the scan prints this
   legend). It also emits **health signals** you must surface in the report:
   `BROKEN-SYMLINK` / `MISSING-SYMLINK` (a rented skill whose `~/.claude/skills` symlink is
   dangling or absent — we rent it but it isn't wired up, so it was *not* diffed),
   `WARN collision` (one skill name in two upstream categories), and `[upstream moved to
   <cat>/]` (a rented skill upstream-relocated out of the watched categories).
   - Done when: every rented match shows a status and every upstream skill is
     matched-or-`NEW`. The printed HEAD sha/date is your dateline for the report.

2. **Classify** each non-identical skill into one bucket, reconciling **every** diff line
   against `manifest.md`:
   - **upstream-ahead** — all diff content is on upstream's side; we carry no delta here.
   - **protected-delta** — the only difference is a delta listed in `manifest.md`.
     Expected, not drift.
   - **both** — upstream changed *and* we carry a delta. Separate the two.
   - **Direction isn't always obvious.** A local-side (`<`) line not in `manifest.md` is
     not automatically ours — it may be pre-sync upstream state that upstream later changed
     (e.g. an old default they since removed). Check the file's history in the upstream
     cache — `git -C "${TMPDIR:-/tmp}/matt-drift-watch-cache" log`/`show` on the path — to
     tell an intentional local delta from an upstream-ahead change; they go in different
     buckets and get opposite verdicts.
   - Done when: every diff line is accounted for. A local-side difference **not** in
     `manifest.md` is itself a finding — an undocumented delta — surface it, don't
     silently absorb it.

3. **Judge reflect-value (rent-vs-own).** For each upstream-ahead / both change and each
   `NEW` skill, decide whether reflecting it earns its keep *given our decomposition and
   vendoring* — not whether the text differs. A rented change is worth reflecting only when
   it sharpens a skill we actually rely on; cosmetic rewords, and skills that describe
   Matt's full idea→ship flow rather than our gate (the reason `ask-matt`/`implement` stay
   unadopted), are not. Skip anything already settled in `manifest.md`.
   - Done when: each change carries a verdict (reflect / skip) and a one-line rationale.

4. **Report — advisory only.** Emit the buckets, the per-change verdicts, and the dateline.
   Then stop.
   - **Never sync, never commit from this skill.** If the human later chooses to reflect a
     rented change, it goes into the unversioned rented layer (`~/.agents/skills/<skill>/`)
     *only* — never claude-config or keel, and never over a protected delta. State this in
     the report; do not act on it.
   - Done when: every skill sits under exactly one bucket and nothing has been modified.

## After the run

If a run establishes a new standing fact — a fresh protected delta, or a decision to keep
ignoring a `NEW` skill — record it in `manifest.md` so the next run doesn't re-litigate it.
