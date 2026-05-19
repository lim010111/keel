# README Style Guide

## Common mistakes

- **No install steps** — never assume setup is obvious
- **No examples** — show, don't just tell
- **Wall of text** — use headers, tables, lists
- **Stale content** — add "last reviewed" date for internal/config projects
- **Generic tone** — write for *your* audience
- **Marketing without anchors** — every adjective ("fast", "robust") needs a referent

## Prose

- Short sentences. One idea per sentence.
- Code blocks for every command — even single-line ones.
- Tables for anything that's a key/value or category/option mapping.
- Link to source files with relative paths (`src/foo.ts`), not absolute URLs that rot.

## Bilingual specifics

- Translate semantically, not literally. Idioms like "out of the box" become "기본 제공" not "박스 밖에서".
- Code blocks, commands, file paths, identifiers stay identical across EN and KO.
- The language switch line is the first non-title line in each file.
- If a section depends on cultural context (e.g. Korean payment integrations), the KO version may expand on it; the EN version notes it briefly and links over.

## Things to omit

- AI-pattern phrases ("In today's fast-paced world", "delve into")
- Praise of the project ("a powerful tool that…")
- Roadmap items not actually planned
- Acknowledgments to unnamed contributors
