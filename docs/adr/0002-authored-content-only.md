# keel ships only authored content; third-party skills are referenced, not vendored

Most of `~/.claude/skills/` is third-party — 14 skills symlinked from a skill
manager. keel contains only content the author wrote; everything else is listed
in `docs/DEPENDENCIES.md` with its upstream source so a reader installs it
themselves.

We rejected mirroring everything because (1) the symlinks point outside the
repo and break on a fresh clone, (2) dereferencing them into real files is
redistribution of others' code under licenses we have not cleared — a repo with
no LICENSE defaults to all-rights-reserved, (3) it would reproduce exactly the
"too heavy" bundle keel exists to avoid. Referencing instead of vendoring keeps
keel light and legally clean.
