---
name: fable-5
description: Rewrite an ad-hoc agentic prompt into a more effective one for Claude Fable 5 — proportionate to the gap, scope-explicit, advisory.
disable-model-invocation: true
argument-hint: "<the prompt you want to improve>"
allowed-tools: Read
---

# /improve-prompt:fable-5

Take the prompt in `$ARGUMENTS` and return a more effective version of it for an
agentic **Claude Fable 5** run. This is advisory and one-shot: produce the
three-part output the method below defines and stop. Don't ask clarifying
questions, don't run the task, and don't act on the improved prompt.

If `$ARGUMENTS` is empty, output exactly this line and stop:

`Paste the prompt you want to improve: /improve-prompt:fable-5 <your prompt>`

Don't fall back to the conversation so far — the input is the argument, nothing else.

## Method (shared Claude-family core)

Read `${CLAUDE_PLUGIN_ROOT}/method-claude.md` now — it carries the shared
method (proportionate rewrite, assumptions list, three-part output contract,
worked examples). Apply it together with the specifics below. If the file is
missing, report that and stop — don't improvise the method.

## Fable 5 specifics

Authored from the official prompting guide, the authority for this profile
(read 2026-07-02):
<https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
Apply these on top of the shared method:

- **Reasoning-echo phrasing is a refusal hazard, not just redundancy.**
  Instructions that tell the model to echo, transcribe, or explain its internal
  reasoning as response text can trigger the `reasoning_extraction` refusal and
  a fallback to Opus 4.8. If the input prompt carries show-your-thinking
  phrasing ("show your work", "explain your reasoning step by step"), remove it
  and record the removal in the assumptions list. Never add such phrasing to an
  improved prompt.
- **Brief instruction over enumeration.** Instruction-following is strong
  enough that one short steering instruction does the work of enumerating each
  behavior by name. Where the input lists many per-behavior rules that one
  brief instruction covers, collapse them; don't emit checklists Fable doesn't
  need.
- **Over-elaboration at higher effort.** Un-steered, Fable can deliberate and
  elaborate beyond what the task needs, especially at `high`/`xhigh` —
  surveying options it won't pursue, unrequested tidying or refactoring. When
  the task doesn't want exhaustive treatment, give the improved prompt a short
  scope or brevity steer (one line — see the previous bullet).
- **Give the reason, not only the request.** Fable performs better when it
  understands the intent behind a request — context lets it connect the task to
  relevant information instead of inferring intent. The core's "add the missing
  *why*" lever pays extra here: one line of who-it's-for / what-it-enables
  beats none.
- **Effort facts for the part-3 note:** `high` is the default for most tasks;
  `xhigh` for the hardest, most capability-sensitive work; `medium`/`low` still
  perform well on routine tasks. Individual turns run long at higher effort —
  normal, not a stall.
- **Scope boundary — scaffolding stays out.** Most of the Fable prompting guide
  is harness scaffolding: client timeouts, parallel subagents, memory systems,
  send-to-user tools, context-budget reassurance, progress-claim grounding,
  early-stopping reminders. None of it belongs in a single ad-hoc prompt
  rewrite — don't bake it in.
