# Tailor

A Claude Code plugin that rewrites the prompt you were about to send — for the
model you're about to send it to.

    /tailor:gpt-5-6-sol  <prompt>
    /tailor:opus-5       <prompt>
    /tailor:opus-4-8     <prompt>
    /tailor:fable-5      <prompt>

The name after the colon is **the model that will run your prompt**, not the
model doing the rewriting. Tailor runs inside Claude Code; the prompt it hands
back is for wherever you're going to run it.

## Why this exists

**The same prompt doesn't work the same way on every model.** Model makers
publish guides on how to prompt their own models, and the advice genuinely
conflicts:

- Claude Opus 5 tends to do *more* than you asked — extra steps, extra checks,
  longer answers. So a good prompt for it draws edges: what's out of scope, what
  to leave alone.
- Claude Opus 4.8 does *exactly* what you asked and nothing beyond it. Tell it to
  fix the first section and it will fix only the first section. So a good prompt
  for it spells out how far each instruction reaches.
- GPT-5.6 gets measurably *better* when you cut — OpenAI's own numbers show
  leaner prompts scoring higher, not just costing less.

One request, three different rewrites. Hardly anyone reads four vendor guides
before typing a sentence into their terminal, and nobody holds them in their head
while typing.

**Your agent's harness doesn't remove this.** Claude Code and Codex do wrap
instructions of their own around your message. But the sentence you type is still
the largest single thing the model reads, and it's the only part that describes
your actual task.

**And that sentence is usually half-formed.** You type it fast, in the middle of
thinking about something else — or you dictate it (Wispr Flow, voice input) and
get filler words and typos on top. What's missing is almost always the same
things: which files, what "done" looks like, what to leave untouched. The model
fills those gaps by guessing, and each model guesses differently.

Tailor closes that gap in one command.

## What you get back

Three things:

1. **The rewritten prompt** — one clean block to copy. Run it in a fresh session,
   so the model starts from the clean version instead of this conversation.
2. **What it had to guess** — the scope and intent it filled in for you. This is
   often the most useful part. Tailor doesn't stop and interrogate you with
   questions; it writes its guesses down so you can correct the wrong ones before
   you run anything.
3. **An effort note**, when the task is big enough to warrant one — which
   reasoning setting suits the run.

**It often barely changes anything, on purpose.** If your prompt already says
which file, what to change, and what not to touch, you get it back nearly as-is
with a note that no real change was needed. That's the intended behaviour, not a
failure. Tailor rewrites in proportion to what's actually missing — it won't pad
your prompt with structure, caveats, or invented requirements just to look busy.
It sharpens your ask; it doesn't replace your voice.

It also never runs your task. It answers once and stops.

## What makes it different

**Every profile is written from that model's own official prompting guide**, and
the guide's URL is pinned inside the profile. You can click through and check the
advice yourself. Tailor isn't guessing at what a model likes.

**There is deliberately no plain `/tailor`.** You have to name the target model.
That's friction on purpose: a default would quietly go stale as models change,
and you'd get Opus 4.8 advice applied to a GPT run without noticing. Naming the
model is what makes the rewrite specific enough to be worth anything.

## Does it actually work?

Honest answer: **not measured yet.** A blind A/B evaluation — same task, original
prompt versus tailored prompt, judged without knowing which is which — is in
progress and unfinished.

So what's claimed here is *where the advice comes from*, not a proven win. Every
profile traces to a published vendor guide you can read yourself. If you want
measured improvement, wait for the eval. If you want your half-typed request
turned into something that states its own scope, that works today.

## Install

```
/plugin marketplace add lim010111/prompt-tailor
/plugin install tailor@lim010111-plugins
/reload-plugins
```

## Layout

- `method.md` — the **shared core**: the method every profile uses, whatever the
  vendor (rewrite in proportion to the gap, list the assumptions, the three-part
  output, what to strip, worked examples). Lives at the plugin root.
- `skills/<model>/SKILL.md` — one **profile per model version**. Each profile
  loads the core at invocation with an explicit instruction: *Read
  `${CLAUDE_PLUGIN_ROOT}/method.md`*. `${CLAUDE_PLUGIN_ROOT}` is substituted in
  plugin-skill content to the plugin root's absolute path (verified on Claude
  Code 2.1.198, though the skills doc only lists it for hooks/MCP), so the model
  never does relative-path math. Each profile also carries `allowed-tools: Read`
  so the core read doesn't hit a permission prompt. The plugin system does **not**
  auto-concatenate the core into a profile, so this instruction is load-bearing —
  keep it. Two rejected alternatives (verified on 2.1.198): bash injection
  (`` !`cat …` ``) silently killed the whole invocation in headless `-p` runs, and
  a relative `${CLAUDE_SKILL_DIR}/../../` path invited wrong-directory reads.

## Profile discipline (version-pinned, doc-grounded)

- **A model version is immutable**, so a profile pinned to it never decays.
  Profiles are added and pruned, never "updated to the latest model".
- **Profile names must not contain a dot.** Claude Code's command tokenizer fails
  to resolve `/tailor:opus-4.8` (typed or headless; verified on 2.1.198 — the line
  falls through to the model as plain text). Spell the version dot as a dash:
  `opus-4-8`.
- **New model** → add `skills/<token>/SKILL.md`, written **from that model's
  official prompting guide**, with the URL pinned and cited in the profile. Keep
  product- and harness-level guidance out of it (progress narration, subagent
  caps, client timeouts, memory systems): a single ad-hoc prompt is not where that
  belongs.
- **New family** (Gemini, …) → add `skills/<token>/SKILL.md` and nothing else.
  There is deliberately **no per-vendor core**: `method.md` is Tailor's own
  rewrite method, not model guidance, so it has no reason to fork by vendor.
  Everything that does vary — effort ladders, thinking and verbosity knobs,
  family-level hazards — already lives in the profile that names the model.
- **Retired model** → prune its profile directory.
- The core carries **no moving-pointer language** (`latest` / `flagship` / "the
  current model") — that would resurrect the staleness the no-default rule removes.
- **Meta-prompt rule:** every file here executes on whatever model your current
  session runs. Keep all of them free of reasoning-echo phrasing — telling a model
  to reproduce its own reasoning can trip a refusal on some targets.

## Development

This repo is a **mirror**. The source of truth is the author's
`~/.claude/skills/tailor/`, where the directory doubles as a skills-directory
plugin: it contains `.claude-plugin/plugin.json`, so Claude Code loads it in place
as `tailor@skills-dir` — no install step while developing. Reference:
<https://code.claude.com/docs/en/plugins-reference> (§ Skills-directory plugins).

To refresh this mirror:

```
rsync -a --delete --exclude='.git' ~/.claude/skills/tailor/ .
```

## License

MIT — see [LICENSE](LICENSE).
