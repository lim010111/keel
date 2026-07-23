#!/usr/bin/env python3
"""Run every authored (vendored) skill's test suite — the oracle's third leg.

The declared oracle (.claude/tdd-test-cmd) discovers scripts/ and hooks/;
skill suites under skills/*/scripts/ live outside that walk because skill
directory names ("setup-status-harness") are not importable package names.
This runner closes the gap:

- a skill counts only when its top-level directory is a REAL directory —
  symlinked skills are rented (~/.agents, ADR-0031/0032) and never ours to run;
- both authored layouts are collected: sibling `scripts/test_*.py` and the
  nested `scripts/tests/test_*.py` (third-party-review);
- each test FILE runs directly (`python3 test_x.py`) in its own subprocess,
  cwd = its directory. Not `unittest discover`: suites share module names
  (test_*.py, status.py), each self-bootstraps sys.path expecting its own
  directory first, and third-party-review's files are script-style (their
  exit code is the verdict, no TestCase to discover). Every authored test
  file carries the `__main__` guard — direct execution is the one contract
  both styles honor.

Exit 0 iff every suite passes.
"""
import argparse
import subprocess
import sys
from pathlib import Path


def suite_dirs(claude_dir: Path) -> list:
    """Directories holding an authored skill's test_*.py files, sorted."""
    out = []
    skills = claude_dir / "skills"
    if not skills.is_dir():
        return out
    for skill in sorted(skills.iterdir()):
        if skill.is_symlink() or not skill.is_dir():
            continue
        for cand in (skill / "scripts", skill / "scripts" / "tests"):
            if cand.is_dir() and any(cand.glob("test_*.py")):
                out.append(cand)
    return out


def run_suite(d: Path) -> int:
    rc = 0
    for f in sorted(d.glob("test_*.py")):
        r = subprocess.run([sys.executable, str(f)], cwd=str(d))
        rc = rc or r.returncode
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args(argv)

    failed = []
    dirs = suite_dirs(args.claude_dir)
    for d in dirs:
        rel = d.relative_to(args.claude_dir)
        print(f"[skill-tests] {rel}", flush=True)
        if run_suite(d) != 0:
            failed.append(str(rel))
    if failed:
        print(f"[skill-tests] FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"[skill-tests] OK — {len(dirs)} suites")
    return 0


if __name__ == "__main__":
    sys.exit(main())
