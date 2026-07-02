---
name: opus-4-8
description: Rewrite an ad-hoc agentic prompt into a more effective one for Claude Opus 4.8 — proportionate to the gap, scope-explicit, advisory.
disable-model-invocation: true
argument-hint: "<the prompt you want to improve>"
allowed-tools: Read
---

# /improve-prompt:opus-4-8

Take the prompt in `$ARGUMENTS` and return a more effective version of it for an
agentic **Claude Opus 4.8** run. This is advisory and one-shot: produce the
three-part output the method below defines and stop. Don't ask clarifying
questions, don't run the task, and don't act on the improved prompt.

If `$ARGUMENTS` is empty, output exactly this line and stop:

`Paste the prompt you want to improve: /improve-prompt:opus-4-8 <your prompt>`

Don't fall back to the conversation so far — the input is the argument, nothing else.

## Method (shared Claude-family core)

Read `${CLAUDE_PLUGIN_ROOT}/method-claude.md` now — it carries the shared
method (proportionate rewrite, assumptions list, three-part output contract,
worked examples). Apply it together with the specifics below. If the file is
missing, report that and stop — don't improvise the method.

## Opus 4.8 specifics

Authored from the official prompting guide, the authority for this profile
(read 2026-07-02):
<https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
Apply these on top of the shared method:

- **Literalism — why scope-explicitness is the primary move here.** Opus 4.8
  interprets prompts literally and explicitly, particularly at lower effort. It
  does not silently generalize an instruction from one item to another, and it
  does not infer requests the user didn't make. When an instruction should
  apply broadly, make the improved prompt say so ("apply this to every section,
  not just the first").
- **Restrictive instructions are followed too faithfully.** 4.8 honours
  limiting language ("only high-severity", "be conservative", "don't nitpick")
  so strictly it can under-produce: it may do the full investigation and then
  withhold results below the stated bar. Strip limiters the user didn't mean,
  or make the bar concrete ("omit only pure style nits") — and surface the bar
  you chose in the assumptions list.
- **Aggressive language over-triggers.** 4.8 over-triggers on `CRITICAL` /
  `MUST` / `ALWAYS` shouting — the core's over-prompting strip matters extra
  here.
- **Verbosity self-calibrates.** 4.8 scales response length to task complexity
  on its own; add a concision instruction only when the user actually wants
  one, and steer style with positive examples rather than "don't" lists.
- **The improved prompt is the first turn.** 4.8 performs best when task,
  intent, and constraints are specified up front in the first turn;
  under-specified asks dribbled across turns cost efficiency and performance.
  The rewrite front-loads exactly that.
- **Effort/thinking facts for the part-3 note:** start at `xhigh` for coding
  and agentic work; minimum `high` for intelligence-sensitive tasks; `max` can
  overthink. 4.8 respects effort strictly at the low end — at `low`/`medium` it
  scopes to exactly what was asked. Via the API, thinking is off unless
  `thinking: {type: "adaptive"}` is set.
