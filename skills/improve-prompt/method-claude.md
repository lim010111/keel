# improve-prompt method — Claude family core

The shared method behind every `/improve-prompt:<model>` profile. The profile
that loaded this file names the target model and carries its model-specific
levers and hazards; this file carries the model-agnostic method. Apply both
together — the profile modulates this method, it never replaces it.

## What you're improving for

Ad-hoc agentic prompts — the kind a person types to start a task — not reusable
API templates. The target model follows instructions faithfully: it won't infer
work the user didn't ask for. So the single highest-value move is **making scope
and intent explicit**. Everything else is secondary and applies only when the
prompt actually needs it.

## Method

**Scale the rewrite to the gap.** An already-clear one-liner gets a light touch or
none; a vague, multi-step task earns structure. Never expand a prompt for its own
sake, and keep the user's voice — you're sharpening their ask, not replacing it.
This guard binds you at every effort level, including `xhigh`: higher effort buys
a better-judged gap, never a bigger rewrite. If you catch yourself adding
structure, caveats, or assumptions the prompt didn't need, cut back to the gap.

**Surface the scope and intent you had to guess** as the assumptions list,
instead of asking. These are what the target agent would otherwise get wrong:
which files, what "done" means, what's in and out of scope, what to leave
untouched.

**Preserve, don't correct.** If the prompt is contradictory, ambiguous, or looks
mistaken, keep the user's wording and name the problem in the assumptions list.
Don't silently fix their intent.

Apply these secondary levers **only when the material warrants them**:

- Make it clear and direct; cut filler.
- Add the missing *why* (motivation/context) when it would change how the task
  gets done.
- Frame the ask as concrete actions, and say what to do — not only what to avoid.
- Use light XML tags only when the content is structured enough to need them
  (e.g. separating a spec from constraints from examples).

Strip, don't add:

- Remove over-prompting — `CRITICAL: you MUST…` / `ALWAYS` / `NEVER` shouting
  becomes plain instruction.
- Remove invitations to over-engineer ("handle every edge case", "make it
  production-grade and extensible") unless the user asked for that.
- No assistant prefill (unsupported on Claude 4.6+), and no reflexive "think
  step by step" or few-shot scaffolding — adaptive thinking makes them redundant.

## Output

Return three parts. Keep each only as long as it needs to be.

1. **Improved prompt** — a single clean, copy-pasteable fenced block. Tell the
   user to run it in a **fresh session**, so the target agent works from the
   clean prompt rather than this conversation's context. If the prompt is already
   good, return it ~unchanged and say *"No substantive change needed — already
   scope-explicit."*

2. **Assumptions to verify** — the scope/intent guesses you baked in, plus any
   contradictions you surfaced. If there were none, say so; don't pad it.

3. **Effort / thinking note** *(only when the task profile warrants it)* — one
   line of advice for the fresh run (a new session, or an API / subagent / eval
   run): e.g. a multi-step build or refactor suits `high` (or `xhigh`) effort with
   adaptive thinking left on. It's advice for that run — the improver doesn't
   change the current session. Omit this part for simple asks.

## Examples

These show the *range*. Match the size of the change to the size of the gap.

### Already good → near-noop

**In:** `In src/auth/session.ts, refreshToken() swallows the network error around line 40. Make it propagate to the caller instead, and update the one call site in login.ts. Don't touch the retry logic.`

**Improved prompt:**

```
In src/auth/session.ts, refreshToken() swallows the network error around line 40.
Make it propagate to the caller instead, and update the one call site in login.ts.
Don't touch the retry logic.
```

No substantive change needed — already scope-explicit (file, function, the change,
the call site, and an explicit boundary).

**Assumptions to verify:** none.

### Vague multi-step build → structure is earned

**In:** `write a script to backfill the user analytics table from the event logs`

**Improved prompt:**

```
Backfill the `user_analytics` table from the raw event logs.

Context: the table was added last week, so rows before then are missing and the
downstream dashboards are undercounting.

- Source: the event logs (confirm the exact location/format).
- Write the rows `user_analytics` expects from those events; match the existing schema.
- Make it safe to re-run: skip or upsert rows that already exist, don't double-count.
- Backfill all history by default.

Done when a re-run is a no-op and the dashboard counts reconcile with the logs.
```

**Assumptions to verify:**

- Where the event logs live and their format (path? table? S3?).
- The mapping from event → `user_analytics` columns.
- Backfill range (all history vs a recent window).
- Language/runtime, and where the script should live.

**Effort / thinking note:** a multi-step backfill suits `high` effort with adaptive
thinking on; bump to `xhigh` if the event→row mapping turns out to be involved.

### Contradiction → preserve and flag

**In:** `Make the config loader synchronous so startup is faster, and keep all the existing await loadConfig() call sites working.`

**Improved prompt:**

```
Make the config loader synchronous so startup is faster. Keep the existing
`await loadConfig()` call sites working.
```

**Assumptions to verify:**

- A synchronous `loadConfig()` and `await loadConfig()` call sites can coexist
  (`await` on a non-promise is a harmless no-op), so this works as written. But if
  the goal was to *remove* the async overhead, the `await`s are now dead and could
  be cleaned up. Confirm which you want — I kept your wording either way.
