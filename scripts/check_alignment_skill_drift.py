#!/usr/bin/env python3
"""Advisory drift check for the rented alignment-gate skills (self-containment#04).

claude-harness-work self-containment#04 (ADR-0031 §B step 4). When this lands in
claude-config, reference it in commits as `refs self-containment#04`.

The harness RENTS its alignment gate — `grill-me` / `grill-with-docs` from
`mattpocock/skills` (ADR-0004: the gate stays third-party, permanently HITL).
The accepted insurance is to *notice* a behaviour-changing upstream update to the
defining gate rather than depend on it never happening. This is a GLOBAL
`~/.agents` concern, so it is a standalone script — explicitly NOT harness-doctor,
which is target-repo-scoped (ADR-0020).

Mechanism: the `skills` CLI records each skill's `skillFolderHash` in
`~/.agents/.skill-lock.json`. That value is the upstream **git tree object SHA**
of the skill folder (40-hex sha1, via the GitHub git/trees API), NOT the sha256
`computeSkillFolderHash`. So we recompute the LIVE folder's git tree SHA the same
way git does and compare: equal => the folder is byte-identical to what was
installed; different => it drifted (an `npx skills update`, or a local edit).

Advisory only: never exits non-zero in a way that gates a session, and prints a
line ONLY when there is drift (low-noise — wired to global SessionStart). Meant
to run after self-containment#02's pristine restore, so a clean tree reports
nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# The watched set is an EXPLICIT list — the alignment-gate skills only, never
# "all skills" (AC3). These are the rented skills whose silent upstream change
# would alter the defining gate's behaviour.
WATCHED = ["grill-me", "grill-with-docs"]


def agents_dir() -> Path:
    return Path(os.environ.get("ALIGNMENT_DRIFT_AGENTS_DIR",
                               str(Path.home() / ".agents")))


# --- git tree object hashing (the reference algorithm `skillFolderHash` uses) --
def _blob_sha(data: bytes) -> bytes:
    return hashlib.sha1(b"blob %d\0%s" % (len(data), data)).digest()


def _tree_sha(path: Path) -> bytes:
    """sha1 of a git tree object for `path`, byte-for-byte as git computes it:
    entries sorted by name (directories sorted as if they ended in '/'), each
    entry = b'<mode> <name>\\0<20-byte-sha>'. Handles regular/executable files,
    symlinks (mode 120000, blob = link target), and nested dirs (recursion).
    `.git` is skipped, matching git itself."""
    entries = []
    for name in os.listdir(path):
        if name == ".git":
            continue
        full = path / name
        if full.is_symlink():
            sha = _blob_sha(os.readlink(full).encode())
            entries.append((name, b"120000", name, sha))
        elif full.is_dir():
            entries.append((name + "/", b"40000", name, _tree_sha(full)))
        elif full.is_file():
            mode = b"100755" if os.access(full, os.X_OK) else b"100644"
            entries.append((name, mode, name, _blob_sha(full.read_bytes())))
    entries.sort(key=lambda e: e[0])
    body = b"".join(mode + b" " + name.encode() + b"\0" + sha
                    for (_k, mode, name, sha) in entries)
    return hashlib.sha1(b"tree %d\0%s" % (len(body), body)).digest()


def folder_hash(path: Path) -> str:
    """The git tree object SHA (40-hex) of a skill folder."""
    return _tree_sha(path).hex()


def _load_lock(ad: Path) -> dict:
    try:
        return json.loads((ad / ".skill-lock.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_drift(ad: Path) -> list[dict]:
    """Return one entry per WATCHED skill whose live folder hash != the recorded
    skillFolderHash. Fail-soft: a missing lock, a skill absent from the lock or
    without a recorded hash, or an unreadable folder is skipped (never a false
    'drift' that cries wolf on a partial install)."""
    lock = _load_lock(ad)
    skills = lock.get("skills", {}) if isinstance(lock, dict) else {}
    out: list[dict] = []
    for name in WATCHED:
        recorded = (skills.get(name) or {}).get("skillFolderHash")
        if not recorded:
            continue  # not tracked here -> nothing to compare against
        folder = ad / "skills" / name
        if not folder.is_dir():
            continue  # not installed -> a missing skill is not "drift"
        try:
            live = folder_hash(folder)
        except OSError:
            continue
        if live != recorded:
            out.append({"skill": name, "recorded": recorded, "live": live})
    return out


def main() -> None:
    try:
        drift = find_drift(agents_dir())
    except Exception:
        sys.exit(0)  # advisory: never gate a session on an infra error
    if drift:
        names = ", ".join(d["skill"] for d in drift)
        print(f"⚠ alignment-skill drift: {names} differs from the recorded "
              f"lockfile hash — a rented grilling skill changed underfoot "
              f"(`npx skills update`?). Review before relying on the gate; see "
              f"docs/adr/0031 §B step 4.")
    sys.exit(0)


if __name__ == "__main__":
    main()
