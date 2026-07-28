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

Exit 0 iff every suite passes AND at least --min-suites were collected.
"""
import argparse
import subprocess
import sys
from pathlib import Path

# The authored suite count — the default collection floor. Bump when an authored
# skill gains its first suite; a DROP below this is a discovery regression, which
# must be loud rather than a vacuous green (see --min-suites).
EXPECTED_SUITES = 6

# Per-file wall-clock bound for the standalone CLI path.
SUITE_TIMEOUT_SECONDS = 300


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


def run_suite(d: Path, timeout: float = SUITE_TIMEOUT_SECONDS) -> int:
    rc = 0
    for f in sorted(d.glob("test_*.py")):
        try:
            r = subprocess.run([sys.executable, str(f)], cwd=str(d),
                               timeout=timeout)
            rc = rc or r.returncode
        except subprocess.TimeoutExpired:
            # Bounded standalone too: inside the Stop hook tdd_verify's own
            # ORACLE_TIMEOUT_SECONDS + killpg caps this, but the module is
            # documented and tested as a CLI, where a wedged suite hung forever.
            print(f"[skill-tests] TIMEOUT after {timeout}s: {f.name}",
                  file=sys.stderr)
            rc = rc or 124
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    # A FLOOR, not an equality: adding an authored suite is fine, losing one is
    # loud. Without it `main` returned 0 on an empty collection — so anything that
    # broke discovery (skills tree moved, --claude-dir resolving through an
    # unexpected parent, every skill symlinked after an ~/.agents re-link, the
    # scripts/ convention renamed) turned this whole oracle leg green while
    # covering nothing. That is the exact failure the leg was added to close.
    ap.add_argument("--min-suites", type=int, default=EXPECTED_SUITES,
                    help=f"fail if fewer than N suites are collected "
                         f"(default {EXPECTED_SUITES}, the authored count)")
    args = ap.parse_args(argv)

    failed = []
    dirs = suite_dirs(args.claude_dir)
    if len(dirs) < args.min_suites:
        print(f"[skill-tests] FAILED: collected {len(dirs)} suites, expected at "
              f"least {args.min_suites} — discovery is broken, not the suites "
              f"(searched {args.claude_dir / 'skills'})", file=sys.stderr)
        return 1
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
