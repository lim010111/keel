{{COMMON}}

# What you are reviewing

The subject is a set of artifacts the human has put forward for independent review —
source files, design documents, or other work products. There is no conversation to
judge here; judge the artifacts themselves, on their own merits. Is the content
correct, well-founded, and internally consistent? What is fragile, missing, or
wrong, and what will cause fallout later?

# Inputs and evidence rules

The artifacts under review have been copied in full to
`{{PROJECT_ROOT}}/.tpr/targets/`. Read each one first and in full:

{{TARGETS_MANIFEST}}

You may also read the surrounding project source at `{{PROJECT_ROOT}}`, read-only, to
verify or contextualize a claim in the artifacts. Keep your focus on the artifacts
under review — do not drift into an unprompted whole-project audit.

# Output

Write your assessment in Korean, as Markdown, in exactly these four sections. Use
plain sentences; keep any bullets flat, no nesting.

1. **평결** — one line: overall verdict plus a risk level of 낮음 / 중간 / 높음.
2. **핵심 결함·리스크** — the core. For each defect: what it is, where (file:line or
   section), the fallout you predict, and a severity. Write "없음" if none.
3. **놓친 대안** — better approaches or directions the artifacts missed. Write "없음"
   if none.
4. **제3자라면 멈췄을 지점** — unresolved decisions or hidden assumptions where an
   independent reviewer would have stopped and pushed back.

Produce the assessment as your final response, then stop.
