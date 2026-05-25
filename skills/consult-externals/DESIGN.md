# Design notes

Why `consult-externals` is built the way it is. Sister document to `third-party-review`'s DESIGN.md — many decisions echo it deliberately.

## What this skill does

At a single grilling branch (typically inside `/grill-me` or `/grill-with-docs`), pull two **external** models (Codex, agy) into the loop as independent advisors on ONE decision. They cannot edit anything. They each produce a structured five-section report. The Main Session reads both clean reports and a side-by-side synthesis, then continues grilling.

The skill is a council, not a judge: it never picks a winner. Disagreement between the two advisors is the load-bearing signal.

## Why a council vs. a single advisor

A single advisor lets the Main agent rationalize the answer it already wanted ("the model agrees with me"). Two advisors trained on different objectives (Codex/OpenAI vs. agy) disagree often enough that the Main agent has to actually engage with the conflict. The skill enforces this by **never merging or paraphrasing** the two reports toward each other.

The model-family diversity is the same reason `third-party-review` uses Codex+Gemini instead of a Claude subagent: same-family advisors are weak third parties.

## Why a single shared prompt

Both agents receive the **identical** prompt. Otherwise their reports aren't comparable — disagreement could be prompt-induced, not substance-induced. The trade-off: a single prompt cannot exploit either CLI's idiosyncrasies. Acceptable; this skill is about substance, not about getting each model to shine.

## Why the prompt is modular

The prompt template follows OpenAI's prompt guidance: Role / Goal / Read-only contract / Context / Decision / Options / Tentative recommendation / Grounding requirement / Output / Stop rules. Modular blocks let the agent pick up context cold (no conversation history) and produce output that maps 1:1 to a contract the extractor can verify.

A "Grounding requirement" block exists specifically because external models, when given a tight focused prompt, will judge based on the Context summary alone unless told otherwise. We need them to actually read the repo — the requester's evidence might be wrong, and only direct file reads catch that. (Validated in the first real run: agy's verdict turned on the Main agent's example cluster being dead links, which only direct `find` revealed.)

## Why the prompt allows `reject framing` as a verdict

In one of the early real-run grilling sessions, both Codex and agy endorsed
the Main agent's tentative recommendation without questioning whether the
decision was load-bearing in the first place. The user pointed out that the
Main agent had constructed a problem that, on closer look, had no observed
failure case in the repo or the conversation — yet the externals worked
inside that framing because the prompt told them to.

The cause was structural. The original prompt's `What I want from you`
section said "Judge the tentative recommendation on its own terms" and
"Hedging ('it depends') without a concrete pick is not [welcome]". Together
those lines made meta-skepticism look like the forbidden category. So the
externals stayed inside the four listed options even when the framing itself
was the thing to question.

The fix (council round 5, 2026-05-25):

- Added a top-level `# Success criteria` section, modelled on OpenAI's
  prompt-guidance recommendation to separate "what counts as a good answer"
  from "what the task is". Two of its four bullets — *Framing-aware* and
  *Specific on rejection* — explicitly permit framing-level disagreement
  but only when grounded in the same missing-evidence pattern that an
  endorsement would require.
- Expanded the Verdict line shape from two options (`endorse <option>` /
  `replace with <new option>`) to three, adding `reject framing` as a
  first-class verdict. Same evidence bar applies.
- Softened the "Disagreement is welcome..." bullet to include framing-level
  disagreement, while still disallowing vague contrarianism.

The asymmetry is deliberate. `reject framing` is *available* but not
*default*: it requires (a) the missing-evidence pattern you searched for
and could not find, and (b) what would change your mind. So the prompt
does not bias the externals toward reflexive criticism — it only gives them
a properly-typed verdict slot for the case where the honest answer is
"this decision is not load-bearing." SUMMARY_FORMAT carries a new
`Framing rejected by:` row so the Main agent sees that signal on equal
footing with `Agreement` and `Better alternative proposed?`.

The cheapest falsification: re-run a council on a deliberately-flawed
framing and confirm at least one external picks `reject framing` with a
specific missing-evidence pattern. If both still endorse, the prompt
boost did not actually shift their behaviour and a stronger mechanism is
needed.

## Why read-only

The advisors are judges, not workers. They read; they do not change state. Two layers of enforcement:

- **Codex**: `--sandbox read-only` enforces this at the runtime level. Always set. Also `--skip-git-repo-check` because read-only sandbox makes the git-repo guard redundant, and a personal harness like `~/.claude` is often consulted from a non-repo cwd.
- **agy**: no read-only flag exists. The `# Read-only contract` block in the prompt is the only constraint. This is fragile (it depends on the model obeying), so the prompt names it explicitly and the dispatcher logs raw output for after-the-fact audit. If agy ever starts mutating files, the raw file is the evidence.

## Why parallel + `wait`

Each CLI run takes 30 s – 5 min. Serial would be 1–10 min total; parallel is the longer of the two. The dispatcher launches both via `&`, then `wait`s on both PIDs. Critical: the dispatcher must NOT exit before the children — otherwise the harness's `run_in_background=true` notification fires too early and the caller has no signal for "both done". The first iteration of this script got this wrong; fixed by adding `wait`.

## Why raw + clean reports

Codex streams a large reasoning trace (tool calls, intermediate analyses, drafts) before its final answer — a real run can be 100–200 KB / 2000+ lines. Pulling that into the Main Session's context costs tokens and noise for ~20 lines of actual content.

Two files per agent:

- `<TS>-<agent>.raw.md` — full stdout/stderr. Source of truth, audit trail.
- `<TS>-<agent>.md` — slice from the LAST `## Verdict` line to EOF. What the Main Session reads.

`Last` not `first` because Codex emits intermediate `## Verdict` drafts during reasoning; the authoritative one is the final occurrence.

## Why verify before presenting

Mirrors `third-party-review`'s "결과 검증 후 제시 — 순서가 핵심" rule. The extractor checks the clean slice contains all required section headers (`## Verdict`, `## Reasoning`, `## Per-option assessment`, `## Risks I cannot evaluate`). If anything is missing, the clean file is replaced with a `FAILED:` marker pointing to the raw. The skill then surfaces the failure honestly instead of presenting a malformed evaluation as if it were valid.

This means an authentication failure, an empty output, or an agent that ignored the contract all produce the same visible signal: the Main Session knows that advisor failed and reasons accordingly.

## What the skill explicitly does not do

- **Does not pick a winner.** Disagreement is the signal.
- **Does not paraphrase one report toward the other.** Merging contaminates evidence.
- **Does not retry on failure.** A failed advisor is a data point, not a problem to paper over.
- **Does not invoke itself recursively.** A council cannot consult another council.
- **Does not own the decision.** The Main Session, after reading the synthesis, returns to the user-facing grilling loop.

## When to skip

Trivial, reversible, or already-settled decisions. The skill spends real API quota and real wall time on both Codex and agy; using it for an obvious call is waste. The grilling skills' own recommendations are usually fine — `consult-externals` is for the branches where the Main agent honestly suspects its recommendation is fragile.

## Why a session-JSONL slice (council 2 + 3)

The first version of this skill let the Main agent hand-write every block in the council prompt — Topic, Context, Options, Tentative recommendation. That made the Main agent the editor of its own evidence, the same anti-pattern `third-party-review`'s DESIGN.md flagged: "평가 대상(main agent)이 증거를 편집하면 평가가 무의미하기 때문." The Grounding requirement caught **repo-claim** edits (e.g., the dead-link cluster the agent had cited as evidence) but had no view into **conversational** edits — an option the user had endorsed two turns ago could be silently omitted and the council would never know.

The decision (council round 2, 2026-05-25): keep the explicit framing blocks (the agent's framing of THIS decision *is* the question) AND additionally embed an automatically-extracted slice of recent conversation, taken straight from the live session JSONL. The council sees both layers and can spot drift between them.

The slice boundary mechanism (council round 3, same day) is the simplest contract that survives empirical probing of the JSONL:

- **Skill tool_use** is the only system-enforced marker. Slash-command-name tags do not appear when a grilling skill is launched via the Skill tool; free-text matching on "grill" gets false positives on quoted occurrences (the script's own code, prior council output, etc.). So the marker is `tool_use.name == "Skill" && input.skill ∈ {grill-me, grill-with-docs}` on the active path, full stop.
- **K = 15** backward human turns is a practical search budget. Grilling sessions longer than that without a Skill-tool relaunch are unusual; longer searches would mostly find empty results in a degraded JSONL.
- **N = 5** human-turn fallback when the marker is not found. Five turns is enough to capture the immediate framing context without bloating the prompt. The boundary mode is reported in the excerpt header so the council knows whether the slice is precise or approximate.

What goes into the slice is intentionally narrow: human text, AI text, AI thinking (truncated), and Skill-tool transitions. Non-Skill `tool_use` and all `tool_result` bodies are dropped — the slice is about what was **said**, not what was **done**. File reads, intermediate searches, and command outputs would otherwise dominate the volume without informing the framing-drift question the slice is meant to answer.

The strip filters (`SYSREMINDER`, `TASK_NOTIFICATION`, `SKILL_INJECT`) were each added in response to a specific noise source observed in real session JSONLs. `SKILL_INJECT` in particular catches the harness's full-SKILL.md preamble that appears as a user-role message immediately after every slash-command invocation — ~5 KB of pure noise per call, dwarfing the actual user intent (which the preceding `<command-name>` block already conveys).

This approach is deliberately lighter than `third-party-review`'s S1–S6 staged reduction. That skill is sized for evaluating an entire session and needs a token-budget controller (`reduction_config.py` with `TARGET_TOKENS = 120_000`). `consult-externals` evaluates one branch and is hard-bounded by the K-turn marker search — most slices end up under 20 KB without any explicit budget guard. If that proves wrong in practice, a token budget can be layered in later as the `δ` guard council 3 suggested.
