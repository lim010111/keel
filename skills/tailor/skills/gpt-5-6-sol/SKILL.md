---
name: gpt-5-6-sol
description: Rewrite an ad-hoc agentic prompt into a more effective one for OpenAI GPT-5.6-sol — proportionate to the gap, scope-explicit, advisory.
disable-model-invocation: true
argument-hint: "<the prompt you want to improve>"
allowed-tools: Read
---

# /tailor:gpt-5-6-sol

Take the prompt in `$ARGUMENTS` and return a more effective version of it for an
agentic **GPT-5.6-sol** run. This is advisory and one-shot: produce the
three-part output the method below defines and stop. Don't ask clarifying
questions, don't run the task, and don't act on the improved prompt.

If `$ARGUMENTS` is empty, output exactly this line and stop:

`Paste the prompt you want to improve: /tailor:gpt-5-6-sol <your prompt>`

Don't fall back to the conversation so far — the input is the argument, nothing else.

## Method (shared core)

Read `${CLAUDE_PLUGIN_ROOT}/method.md` now — it carries the shared method
(proportionate rewrite, assumptions list, three-part output contract, worked
examples). Apply it together with the specifics below. If the file is missing,
report that and stop — don't improvise the method.

## GPT-5.6 specifics

Authored from OpenAI's model guidance for GPT-5.6, the authority for this
profile (read 2026-07-25):
<https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6>.
That guidance covers the 5.6 line as a whole; this profile targets `gpt-5.6-sol`,
the flagship. Apply these on top of the shared method:

- **Lean is this profile's spine, and it is measured.** The guide reports that
  leaner system prompts improved eval scores by roughly 10–15% while cutting
  tokens 41–66% and cost 33–67%. So the core's proportionality guard isn't only
  economy here — on GPT-5.6 the shorter prompt also scores *better*. State each
  instruction once, drop repeated examples, and delete process steps the model
  already handles. Between adding a clause and cutting one, cut.
- **Outcome, not choreography.** Say what a finished result looks like, what
  bounds it, and when to stop — rather than scripting the steps to get there.
  5.6 infers the underlying goal from context better than its predecessors, so
  narrating a procedure it already knows is the most common dead weight to
  remove.
- **One authorization line, not scattered "ask first" rules.** Where the prompt
  is vague about how far the agent may go on its own, name the safe local
  actions (reading files, inspecting logs, editing in-scope code, running tests)
  and what needs confirmation — once, in one place. Repeating "ask first" /
  "don't mutate" / "wait for approval" across a prompt is exactly the bloat the
  lean finding penalises.
- **It runs short by default — ask for more, not less.** The guide states 5.6
  tends to be more concise by default than 5.5, so brevity boilerplate ("be
  concise", "keep it short") is usually dead weight to strip. If the user wants
  depth, evidence, or a long document, the improved prompt has to say so. Put
  *what must be covered* in the prompt; the global detail dial is `text.verbosity`
  and belongs in the part-3 note, not the prompt body.
- **Define tone by its choices, not by a label.** Broad labels ("friendly",
  "empathetic") are ambiguous; spell out the behaviour instead.
- **Effort facts for the part-3 note:** the ladder is `none`, `low`, `medium`,
  `high`, `xhigh`, `max`, and omitting it gives `medium` in both standard and
  pro modes. `low` suits latency-sensitive work; `high`/`xhigh` when reasoning
  buys a measured quality gain; `max` for quality-first hardest work; from
  `none`, try `low` once the task benefits from reasoning or tools. The guide's
  own tuning heuristic is worth passing on — keep the effort you use today, then
  test one level lower. `reasoning.mode: "pro"` is independent of effort and
  worth naming for a hard, high-stakes review or analysis, not for routine or
  latency-sensitive asks.
- **Scope boundary — plumbing stays out.** Programmatic Tool Calling and its
  `<tool_orchestration>` block, multi-agent subagent coordination, prompt caching
  (explicit breakpoints, TTL), persisted reasoning (`reasoning.context`),
  `safety_identifier`, image-detail settings, and Responses API migration
  mechanics are all product- and harness-level. None of it belongs in a single
  ad-hoc prompt rewrite — don't bake it in.

## Doc wording you can lift

Three of the levers above ship ready-made text. These are written for a system
prompt, so on an ad-hoc ask a single clause usually carries the value — pasting a
full paragraph into a small request breaks the core's proportionality guard, and
on this target it costs quality too.

**Authorization boundary:**

> For requests to answer, explain, review, diagnose, or plan, inspect the
> relevant materials and report the result. Do not implement changes unless the
> request also asks for them. For requests to change, build, or fix, make the
> requested in-scope local changes and run relevant non-destructive validation
> without asking first. Require confirmation for external writes, destructive
> actions, purchases, or a material expansion of scope.

**Short-answer priority order:**

> Lead with the conclusion. Include the evidence needed to support it, any
> material caveat, and the next action. Omit secondary detail and repetition.

**Tone as choices:**

> State the answer directly. If the user reports a problem, acknowledge the
> specific issue before giving the next step. Use reassurance only when it is
> relevant. Omit generic praise and unnecessary sign-offs.

## The shape to aim at

The guide's pro-mode example is a compact model of the outcome-first shape —
artifact, failure classes, what each finding must carry, and a stopping
condition, with no procedure scripted:

> Review this database migration plan for failure modes that could cause data
> loss or extended downtime. For each finding, cite the relevant step, estimate
> impact and likelihood, and recommend a specific mitigation. Return the five
> most important risks in severity order.
