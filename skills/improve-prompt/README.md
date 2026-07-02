# improve-prompt — colon-namespaced prompt-improver plugin

Rewrites an ad-hoc agentic prompt for a **named** target model:

    /improve-prompt:opus-4-8 <prompt>
    /improve-prompt:fable-5  <prompt>

There is deliberately **no bare `/improve-prompt`** and no default target —
naming the target is enforced structurally (ADR-0035, plan repo). The method is
proportionate, not maximal (ADR-0029).

## How it loads (skills-directory plugin)

This directory sits under `~/.claude/skills/` and contains
`.claude-plugin/plugin.json`, which makes Claude Code load it **in place** as
the plugin `improve-prompt@skills-dir` on the next session — no marketplace, no
install step, no copy into the plugin cache. (`claude plugin init` scaffolds
this same shape; this plugin was authored directly.) After editing, start a new
session or run `/reload-plugins`. Reference:
<https://code.claude.com/docs/en/plugins-reference> (§ Skills-directory
plugins), verified on Claude Code 2.1.198.

## Layout

- `method-claude.md` — the **Claude-family core**: the model-agnostic method
  (proportionate rewrite, assumptions list, three-part output, strips, worked
  examples). Lives at the plugin root.
- `skills/<model>/SKILL.md` — one **version-pinned profile** per target model.
  Each profile loads the core at invocation with an explicit instruction:
  *Read `${CLAUDE_PLUGIN_ROOT}/method-claude.md`*. `${CLAUDE_PLUGIN_ROOT}` is
  substituted in plugin-skill content to the plugin root's absolute path
  (verified on 2.1.198, though the skills doc only lists it for hooks/MCP), so
  the model never does relative-path math. Each profile also carries
  `allowed-tools: Read` so the core read doesn't hit a permission prompt.
  The plugin system does **not** auto-concatenate the core into a profile, so
  this instruction is load-bearing — keep it. Two rejected alternatives
  (verified on 2.1.198): bash injection (`` !`cat …` ``) silently killed the
  whole invocation in headless `-p` runs, and a relative
  `${CLAUDE_SKILL_DIR}/../../` path invited wrong-directory reads by the model.

## Profile discipline (version-pinned, doc-grounded)

- **A model version is immutable**, so a profile pinned to it never decays.
  Profiles are added and pruned, never "updated to the latest model".
- **Profile tokens must not contain a dot.** Claude Code's command tokenizer
  fails to resolve `/improve-prompt:opus-4.8` (typed or headless; verified on
  2.1.198 — the line falls through to the model as plain text). Spell the
  version dot as a dash: `opus-4-8`.
- **New model** → add `skills/<token>/SKILL.md`. Author it **from the model's
  official prompting doc** — pin and cite the doc URL in the profile. Use the
  scope filter in the plan repo
  (`.scratch/prompt-improver/references/model-prompting-scope-filter.md`) to
  keep harness scaffolding out of a single-prompt rewrite.
- **New family** (OpenAI, Gemini, …) → add `method-<vendor>.md` +
  `skills/<token>/SKILL.md`. Zero renames or moves of existing files.
- **Retired model** → prune its profile directory.
- The core carries **no moving-pointer language** (`latest` / `flagship` /
  "the current model") — that would resurrect the default-staleness the
  no-default rule removes.
- **Meta-prompt rule:** every file here executes on the current session model,
  possibly Fable 5. Keep all of them free of reasoning-echo phrasing — telling
  the model to reproduce its reasoning trips the `reasoning_extraction`
  refusal.
