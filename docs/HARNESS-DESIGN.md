# Harness design — gating the SDLC

> Where [`README.md`](../README.md) lists *what keel ships today*, this
> document explains *the shape it is growing into*.

keel started as three layers — norms, enforcement, visibility. That is how the
harness keeps an agent honest *within* a unit of work. This document is about
the layer above it: how the harness gates the software development lifecycle
*between* its phases.

## The idea

An autonomous coding agent does not fail uniformly. It fails at the seams —
where one phase of work hands off to the next. It starts implementing before
it has understood the problem. It calls a change verified when the test it
trusted was quietly weakened. It merges work that no second party ever read.

So the harness is **SDLC-shaped**. Each phase of the lifecycle ends at a
**gate**: a trustworthy success signal that the phase actually produced what
the next phase needs. The agent loops autonomously where that signal is
mechanical, and pauses for human confirmation where the gate hands judgment
to a person.

## What a gate is

A **gate** is a trustworthy success signal for one SDLC phase — one gate per
phase. Two rules give the word its meaning:

- **No gate here is fully automatic.** By default every gate delegates some
  judgment to the human. The agent loops where the signal is mechanical; the
  human closes the gate where it is not.
- **Every gate is bypassable, on the record.** A gate has an *audited bypass
  lane* — an acknowledged, recorded way to pass without satisfying it (a
  `--no-verify` commit, a bypass label). The harness makes a bypass *visible*
  for scrutiny rather than pretending it cannot happen.

## The three gates

| SDLC phase | Gate | What it verifies | Status |
|---|---|---|---|
| Alignment | Alignment gate | The human↔agent grilling converged on a rich-enough project context | **Shipped** — `/third-party-review` |
| Verification | Self-verification gate | The oracle behind a "tests pass" verdict was not weakened | **Designed** |
| Pre-merge | Merge gate | An independent reviewer read the change before it reached `main` | **In progress** |

### Alignment gate — shipped

The first phase of work is a human grilling the agent until the project
context is rich enough to execute on. The alignment gate verifies that the
grilling actually converged. `/third-party-review` — shipped in this repo —
reduces the session transcript deterministically, hands it to outside models
(Codex, Gemini), and asks whether the human↔agent conversation has drifted
far enough to cause trouble later. Resolving a divergence is the pair's work,
so this gate is closed largely by the human.

### Self-verification gate — designed

When the agent runs the tests and they pass, it has *consulted an oracle*, not
*reviewed its own work*. An **oracle** is an external, deterministic
verdict-producer — test runner, type checker, linter, build. The agent can
trust an oracle's verdict only if the oracle was not **weakened**: a commit
that makes the oracle pass more easily — deleting a test, adding a skip
directive, lowering a coverage threshold. The self-verification gate detects
weakening *structurally*, by diffing test and config files, regardless of the
commit's stated intent. A legitimate weakening simply travels the audited
bypass lane. This gate is designed; it is not yet built.

### Merge gate — in progress

The last gate before `main` is an independent adversarial review of the
change — a reviewer in a *different context* from the one that wrote the code,
because same-context bias makes self-review irreducibly weak. This is keel's
current build focus.

## One shape, many gates

The three gates are not three architectures. They share one uniform shape, so
adding a gate is a new *instance*, not new structure:

```
{ unit, oracle / reviewer, trust signal, soft/hard policy, audited bypass lane }
```

- **unit** — what the gate evaluates (a session transcript, a diff, a PR).
- **oracle / reviewer** — what produces the verdict.
- **trust signal** — the output the next phase relies on.
- **soft/hard policy** — a soft gate warns, a hard gate blocks; a gate is
  promoted from soft to hard once measurement shows it is trustworthy.
- **audited bypass lane** — the recorded escape hatch.

A *setup skill* installs a gate's artifacts into a project; it is never itself
a gate.

## Status

The alignment gate is shipped. The merge gate is being built. The
self-verification gate is designed and deferred. Design rationale and the
issue-level breakdown live in the planning repo; this document is the public
summary of the model.
