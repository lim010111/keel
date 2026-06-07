# keel ships only authored content; third-party skills are referenced, not vendored

`~/.claude/skills/` is a mix of the author's own skills and third-party ones —
15 are symlinked from a skill manager. keel contains only what the author
wrote; everything else is listed in `docs/DEPENDENCIES.md` with its upstream
source so a reader installs it themselves.

We rejected mirroring everything because (1) the symlinks point outside the
repo and break on a fresh clone, (2) dereferencing them into real files is
redistribution of others' code under licenses we have not cleared — a repo with
no LICENSE defaults to all-rights-reserved, and (3) it would blur what keel is:
a curated record of the author's own work, not an undifferentiated copy of the
whole `~/.claude` setup. Referencing instead of vendoring keeps keel authored
and legally clean.
