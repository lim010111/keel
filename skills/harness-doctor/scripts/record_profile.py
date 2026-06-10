#!/usr/bin/env python3
"""record_profile — the Tier-2 [harness] writer for scaffold-doctor #03.

The read-only #02 engine (harness_doctor.py) proposes and computes coverage but
NEVER writes (AC15). This module performs the single filesystem write: recording
the operator-confirmed intended scaffold as a `[harness]` meta-section in the
target repo's harness.toml. The write is a non-destructive, comment-preserving,
section-scoped TEXT merge (AC16) — never a lossy whole-file re-serialize.
"""
import os
import sys
from pathlib import Path

# Resolve toml_sections (the shared section-scoped TOML text utilities) from
# ~/.claude/scripts, the same standalone-subprocess bootstrap auto_fill.py uses:
# <this file> is skills/harness-doctor/scripts/record_profile.py, so parents[3]
# is ~/.claude and parents[3]/scripts holds the shared module.
_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from toml_sections import section_is, split_sections


def _atomic_write_text(path, text):
    """Write `text` to `path` ATOMICALLY: a temp file in the same dir + os.replace,
    so a failure (encode error, disk-full, crash, kill) leaves the ORIGINAL file
    fully intact — NEVER a truncated/empty harness.toml. Path.write_text opens 'w'
    (truncate) then encodes+writes, so any mid-write failure corrupts the canonical
    record in place — destroying exactly the annotated sibling gate sections AC16
    took pains to preserve. Mirrors merge_gate_adjudicate._atomic_write_text /
    merge_gate_local.write_summary_atomic."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def merge_harness_section(existing, profile):
    """Return harness.toml text with `profile` recorded as `[harness]`, merged
    NON-DESTRUCTIVELY into `existing` (AC16). Every sibling section and its inline
    comments survive verbatim; any prior `[harness]` block is dropped and the
    fresh one is appended at the end with newline discipline. Given a valid (or
    empty) `existing`, the result is valid TOML — no header is glued onto a value
    line. (`existing` is read from a `harness.toml` the engine has already parsed,
    so validity is the caller's precondition; this splices text, it does not
    re-validate the sibling blocks it preserves verbatim.)"""
    block = render_harness_section(profile)
    if not existing.strip():
        return block
    # split_sections/section_is come from the shared toml_sections module —
    # section_is folds TOML-equivalent header spellings (`[ harness ]`,
    # `["harness"]`), so an equivalent block is replaced, never duplicated
    # (codex:finding-0).
    kept = "".join("".join(lines) for hdr, lines in split_sections(existing)
                   if not section_is(hdr, "harness"))
    if not kept.strip():
        return block            # existing held only a [harness]/whitespace
    if not kept.endswith("\n"):
        kept += "\n"
    return kept + "\n" + block   # blank line before the appended section


def render_harness_section(profile):
    """The `[harness]` section text for `profile` (intended scaffold + ci
    judgment), annotated so the recorded contract stays auditable. Ends in \\n."""
    items = ", ".join(f'"{s}"' for s in profile.get("scaffold", []))
    lines = [
        "[harness]",
        "# Intended scaffold (scaffold-doctor #03 / ADR-0020 §3) — the coverage",
        "# denominator. Slugs are EXACTLY the doctor's per-concern probe IDs; NOT",
        "# profile-qualified (it is merge-gate, never merge-gate-local).",
        f"scaffold = [{items}]",
    ]
    if profile.get("ci") is not None:
        lines += [
            "# Recorded judgment with no mechanical detector — reported",
            '# "wanted / not-yet-measurable", never a term in the coverage fraction.',
            f"ci = {'true' if profile['ci'] else 'false'}",
        ]
    return "\n".join(lines) + "\n"


def record_profile(repo_root, profile):
    """Record `profile` as the `[harness]` section of <repo_root>/harness.toml —
    the single write in #03 (AC10/AC15). Returns the path written."""
    path = Path(repo_root) / "harness.toml"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _atomic_write_text(path, merge_harness_section(existing, profile))
    return path
