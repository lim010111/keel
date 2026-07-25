---
name: opus-5
description: Rewrite an ad-hoc agentic prompt into a more effective one for Claude Opus 5 — proportionate to the gap, scope-explicit, advisory.
disable-model-invocation: true
argument-hint: "<the prompt you want to improve>"
allowed-tools: Read
---

# /tailor:opus-5

Take the prompt in `$ARGUMENTS` and return a more effective version of it for an
agentic **Claude Opus 5** run. This is advisory and one-shot: produce the
three-part output the method below defines and stop. Don't ask clarifying
questions, don't run the task, and don't act on the improved prompt.

If `$ARGUMENTS` is empty, output exactly this line and stop:

`Paste the prompt you want to improve: /tailor:opus-5 <your prompt>`

Don't fall back to the conversation so far — the input is the argument, nothing else.

## Method (shared core)

Read `${CLAUDE_PLUGIN_ROOT}/method.md` now — it carries the shared
method (proportionate rewrite, assumptions list, three-part output contract,
worked examples). Apply it together with the specifics below. If the file is
missing, report that and stop — don't improvise the method.

## Opus 5 specifics

Authored from the official prompting guide, the authority for this profile
(read 2026-07-25):
<https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>.
Apply these on top of the shared method:

- **Over-delivery is this profile's spine.** Opus 5 takes more initiative than
  Opus 4.8: it expands scope with steps nobody asked for, verifies work nobody
  asked it to verify, and writes longer. So the levers here mostly **bound** the
  task rather than push it — the improved prompt draws the edges. Where the 4.8
  profile fights literalism, this one fights initiative.
- **Bound the scope of narrow tasks.** Scope expansion is the default failure
  mode, so a narrow ask earns an explicit edge — what's out of scope, what to
  leave untouched, where to stop.
- **Strip verification and re-check instructions.** Opus 5 verifies its own work
  and catches its own mistakes unprompted, and explicit instructions compound
  with that into wasted tokens with no quality gain. Remove "include a final
  verification step", "use a subagent to verify", "double-check your answer",
  "re-verify before responding" and note the removal in the assumptions list;
  never add them. Anti-stub language ("don't leave TODOs or placeholders") is a
  no-op for the same reason — it completes full tasks by default.
- **Restrictive limiters under-produce — make it two passes.** On review- and
  audit-shaped asks, "only high-severity", "be conservative", "don't nitpick"
  are followed literally and suppress real findings. The doc's remedy is to ask
  for everything and filter in a separate pass; rewrite that way and record the
  change in the assumptions list.
- **Length must be asked for, not dialed.** Effort governs how much Opus 5
  *thinks*, not how much it *says* — lowering effort won't shorten the response.
  Default responses, and files it writes to disk, both run longer than on prior
  Opus models. If the user wants a brief answer or a tight document, the
  improved prompt has to say so in words.
- **Give the whole task up front, then let it run.** Opus 5 is strongest on
  multi-file features, larger refactors, and end-to-end work when handed the
  complete specification in the first turn rather than dribbled across turns.
  The rewrite front-loads exactly that.
- **Style and template are yours to name.** For spreadsheet, slide, and
  document deliverables, name the style, template, or house format it must
  follow — it won't infer one.
- **Effort facts for the part-3 note:** `high` is the default; `low`/`medium`
  are the primary cost and latency control and hold quality well (including on
  code review, where accuracy survives lower effort — a fast pass now, a
  thorough pass later); `xhigh` for demanding coding and agentic work. Thinking
  is **on by default** (no `thinking: {type: "adaptive"}` needed, unlike 4.8) —
  if cost is the worry, lower the effort rather than disabling thinking.
  Effort defaults carried over from 4.8 are worth re-checking; Opus 5 rarely
  needs to start as high.
- **Scope boundary — scaffolding stays out.** Much of the Opus 5 guide is
  product- and harness-level: narration cadence for progress updates,
  subagent-spawn caps, correction-narration tuning, system-prompt tail
  reminders, and the thinking-disabled artifact mitigations (tool calls leaking
  as text, internal XML tags). None of it belongs in a single ad-hoc prompt
  rewrite — don't bake it in.

## Doc wording you can lift

The guide ships ready-made instruction text for three of the levers above. Lift
or trim these when the gap warrants it — a one-line clause carries most of the
value, and pasting a full paragraph into a small ask breaks the core's
proportionality guard.

**Bounding scope:**

> Deliver what was asked, at the scope intended. Make routine judgment calls
> yourself, and check in only when different readings of the request would lead
> to materially different work. If the request seems mistaken or a better
> approach exists, say so in a sentence and continue with the task as asked
> rather than quietly narrowing, widening, or transforming it. Finish the whole
> task, and stop short of actions that are clearly beyond what was asked.

**Written deliverable length:**

> Match the length of written documents to what the task needs: cover the
> substance, but do not pad with filler sections, redundant summaries, or
> boilerplate.

**Response brevity** (written for a multi-turn product's system prompt — for an
ad-hoc prompt, usually the first sentence alone):

> Keep responses focused, brief, and concise. Keep disclaimers and caveats
> short, and spend most of the response on the main answer. When asked to
> explain something, give a high-level summary unless an in-depth explanation is
> specifically requested.
