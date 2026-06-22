{{COMMON}}

# What you are reviewing

The subject is a Claude Code working session between a human and an AI coding agent
("the main agent"). Judge whether the human and the main agent are actually aligned,
and — equally — whether what they decided and produced is itself sound. Treat
apparent agreement between the human and the agent as suspicious until the transcript
shows it is real.

Judge three axes:

- Human side: are the human's requests, premises, and assumptions sound? Flag wrong
  premises, ill-posed requests, and assumptions stated as fact.
- Agent side: is the main agent's solution and direction appropriate, does it
  actually fit what the human asked for, and is there a better alternative the
  session missed?
- The substance itself: independent of who proposed it and of whether the two agree,
  is the content they settled on — the decisions, the reasoning, the artifacts —
  actually correct and well-founded? A flaw both parties agreed to is still a flaw.

Your central deliverables are (a) the divergences — points where the two believe
they agree but the evidence shows they do not, or where the session committed to a
direction without resolving a real fork — and (b) the substantive defects in what
was produced. Both cause fallout later.

# Inputs and evidence rules

Primary evidence is the session transcript at `{{TRANSCRIPT_PATH}}`. Read it first
and in full. It is chronological; each turn is labeled Human / AI / tool. A JSON
header records what was mechanically elided during reduction (e.g. file-read bodies
truncated to the first {{READ_HEAD_LINES}} lines) — when a judgment depends on
elided content, that is your signal to consult the project.

The transcript is the *active* conversation path only. If the human rewound the
session and resent, the abandoned branches were removed — the main agent never had
them in context, so they are not its responsibility. The header's
`rewound_human_turns` is the count of human turns dropped this way: treat a non-zero
value as a friction signal (the human had to redo work) but never fault the agent
for content on a rewound branch. If `active_path_reconstructed` is false, the active
path could not be rebuilt and rewound branches may still be present — say so in your
verdict and lower your confidence accordingly.

The main agent's `thinking` blocks are included where present. They are high-signal:
they show the agent's private reasoning, which is exactly where it may diverge from
what it told the human. Read them closely.

The project source is at `{{PROJECT_ROOT}}`. You may read it, read-only, to verify or
contextualize a specific claim in the transcript, and to judge the soundness of the
artifacts the session produced. Do not turn this into an unprompted whole-project
audit — follow the transcript. The project is in its final state: it reflects where
the session ended, not what the agent saw at an earlier turn. Use it to judge the
final solution, not mid-session decisions.
{{TARGETS_BLOCK}}
If the transcript's final turn is the invocation of this evaluation itself, ignore
it.

# Output

Write your assessment in Korean, as Markdown, in exactly these six sections. Use
plain sentences; keep any bullets flat, no nesting.

1. **평결** — one line: overall verdict plus a risk level of 낮음 / 중간 / 높음.
2. **축 1 — 인간 쪽** — doubtful requests, premises, or assumptions on the human's
   part. Write "없음" if none.
3. **축 2 — 에이전트 쪽** — problems in the main agent's solution or direction, and
   any better alternative the session missed. Write "없음" if none.
4. **축 3 — 내용·결과물의 타당성** — independent of the human/agent dynamic, defects in
   the substance the session settled on: incorrect decisions, unsound reasoning, and
   flaws in the artifacts produced or discussed. Ground each in the transcript, the
   pinned targets, or the project source. Write "없음" if none.
5. **간극 목록** — the core. For each divergence: what the gap is, the transcript
   turn(s) evidencing it, the fallout you predict, and a severity.
6. **제3자라면 멈췄을 지점** — forks the session passed over without resolving; where
   an independent observer would have stopped and pushed back.

Produce the assessment as your final response, then stop.
