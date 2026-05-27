# Harness design — gating trust transitions

> Where [`README.md`](../README.md) lists *what keel ships today*, this
> document explains *the shape it is growing into*.

keel started as three layers — norms, enforcement, visibility. That is how the
harness keeps an agent honest *within* a unit of work. This document is about
the layer above it: how the harness gates the **trust transitions** an
agent-produced artifact crosses on its way to a wider audience.

## The idea

An autonomous coding agent does not fail uniformly. It fails at the seams —
where the artifact it produced crosses into a wider audience, effect-radius,
authority, or verifier. It starts implementing before the human↔agent context
is rich enough. It calls a change verified when the test it trusted was
quietly weakened. It merges work that no second party ever read.

So the harness is a **change-control plane** organized around those **trust
transitions**. At each transition it places one **gate** — a trustworthy
success signal that the artifact is ready for the next world. Between
transitions is an **implementation work interval** governed by protocols
(TDD, scope discipline, hooks) rather than gates: no trust boundary is
crossed in between, so a gate there would force the agent to grade its own
work against itself.

## What a gate is

A **gate** is a trustworthy success signal for one **trust transition** —
one gate per transition. Two rules give the word its meaning:

- **No gate here is fully automatic.** By default every gate delegates some
  judgment to the human. The agent loops where the signal is mechanical; the
  human closes the gate where it is not.
- **Every gate is bypassable, on the record.** A gate has an *audited bypass
  lane* — an acknowledged, recorded way to pass without satisfying it (a
  `--no-verify` commit, a bypass label). The harness makes a bypass *visible*
  for scrutiny rather than pretending it cannot happen.

## The four gates (v1 shape)

The target taxonomy is `alignment → verification → merge → release → deploy`,
with an **operate loop** feeding observations back into the next alignment.
In a **single-user installation** where `~/.claude/` *is* the runtime — the
developer's environment works directly off the source tree, with no
release/deploy step in between — release and deploy share one Gate, yielding
the v1 shape `alignment → verification → merge → distribution`:

| Trust transition | Gate | What it verifies | Status |
|---|---|---|---|
| Alignment (ad-hoc ask → autonomous-loop context) | Alignment gate | The human↔agent grilling converged on a rich-enough project context | **Shipped** — `/third-party-review` |
| Verification (agent self-claim → independent oracle verdict) | Self-verification gate | The oracle behind a "tests pass" verdict was not weakened | **Designed** |
| Merge (ephemeral branch → main, visible to collaborators) | Merge gate | An independent reviewer read the change before it reached `main` | **In progress** |
| Distribution (main → user runtime; v1 collapse of release + deploy) | Distribution gate | Deferred while user base = {developer} and `~/.claude/` *is* the runtime | **Deferred** |

The distribution collapse splits back into separate **release** and **deploy**
gates once (a) the user base extends beyond the developer, (b) versioned
artifacts (semver, changelog, migration notes) become user-visible, or (c)
deploy-time validation needs a different oracle than release-time.

### Alignment gate — shipped

The first transition is the human↔agent **alignment** — turning an ad-hoc
ask into the context the autonomous loop will rely on. The alignment gate
verifies that the grilling actually converged. `/third-party-review` —
shipped in this repo — reduces the session transcript deterministically,
hands it to outside models (Codex, Gemini), and asks whether the human↔agent
conversation has drifted far enough to cause trouble later. Resolving a
divergence is the pair's work, so this gate is closed largely by the human.

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

### Distribution gate — deferred (v1 collapse of release + deploy)

In a single-user installation where `~/.claude/` *is* the runtime, there is no
release/deploy step between the developer and the running code. Two distinct
verifiers / evidence sets are not warranted yet, so release and deploy share
one **distribution** Gate, currently deferred.

> **Note**: keel itself is *not* an instance of this gate. The public-mirror
> sync from `~/.claude/` into this repo crosses an **audience** boundary
> (private → public), not the developer-runtime boundary that distribution
> covers. The two transitions warrant separate gates and will be designed as
> such if and when keel-publication needs its own trustworthy signal.

## One shape, many gates

Every gate above is the same shape, so adding one is a new *instance*, not new
structure:

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
self-verification gate is designed and deferred. The distribution gate is
deferred under the v1 single-user collapse. Design rationale and the
issue-level breakdown live in the planning repo; this document is the public
summary of the model.
