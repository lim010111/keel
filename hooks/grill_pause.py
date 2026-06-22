#!/usr/bin/env python3
"""PreToolUse hook — pause the narrative guard on a grill-family Skill launch.

claude-harness-work self-containment#02 (ADR-0031 §B step 2, "own the delta,
rent the base"). When this lands in claude-config, reference it in commits as
`refs self-containment#02`.

A grilling session edits ADRs / CONTEXT / issue files inline as decisions settle
(real posture movement) but refreshes the STATUS.md narrative only once grilling
concludes — so narrative_guard's Stop `check` would block the end of every
grilling turn that touched an ADR. The fix is a (session, repo) pause marker that
makes `check` exit 0 until grilling ends.

That marker used to be written by the RENTED grilling skills' own prose
(`grill-with-docs` / `harden-issue` shelling `narrative_guard.py pause`). That
authored delta lived only in the unversioned ~/.agents working tree, so a
`npx skills update` would silently drop it and the false-positive blocks would
return. This hook moves the WRITER to the owned side: it fires as the grill Skill
launches and writes the SAME mutable marker. `narrative_guard.check` is untouched
(it already honours the marker); `resume` (folded into /status's closing step)
re-arms it exactly as before. The rented skills can then be restored pristine.

Contract: PreToolUse must NEVER deny the tool — always exit 0 (a non-zero/`deny`
would block the grilling Skill the user just asked for). Fail-soft like
`narrative_guard.pause`: a marker that can't be written must not derail the
session.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOKS = Path.home() / ".claude" / "hooks"
sys.path.insert(0, str(HOOKS))
import narrative_guard as ng  # noqa: E402  — reuse the exact marker keying `check` reads.

# The grilling skills that bracket their session with a narrative-guard pause.
# An explicit allow-list (not "all skills"): only these defer the narrative.
GRILL_SKILLS = {"grill-me", "grill-with-docs", "harden-issue"}


def main() -> None:
    p = ng.read_input()
    if p.get("tool_name") != "Skill":
        sys.exit(0)
    skill = (p.get("tool_input") or {}).get("skill")
    if skill not in GRILL_SKILLS:
        sys.exit(0)
    # Key the marker the SAME way narrative_guard.check does: session id from the
    # hook payload, repo from repo_root(payload cwd). check() looks up exactly
    # pause_marker_path(session_id, repo_root(cwd)), so this is the path it reads.
    sid = str(p.get("session_id", "")) or "default"
    cwd = p.get("cwd") or os.getcwd()
    try:
        root = ng.repo_root(cwd)
        ng.state_dir().mkdir(parents=True, exist_ok=True)
        ng.pause_marker_path(sid, root).write_text(
            json.dumps({"session": sid, "root": str(root), "via": "grill_pause"}))
    except Exception:
        pass  # fail-soft: never block a grilling launch over a marker-write error
    sys.exit(0)


if __name__ == "__main__":
    main()
