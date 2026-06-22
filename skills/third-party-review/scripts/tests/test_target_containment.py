#!/usr/bin/env python3
"""Containment regression test for prepare_review.py copy_targets().

A third-party-review run stages target bytes into <cwd>/.tpr/targets/ and then
feeds them to EXTERNAL review models (codex/agy/claude). The containment
invariant under test:

    No file whose *real* path resolves OUTSIDE the project cwd may have its
    bytes land under .tpr/targets/.

`copy_targets()` resolves a target with os.path.abspath (which does NOT resolve
symlinks and does NOT verify the path is under cwd), then copy2 / copytree it —
dereferencing symlinks. This test drives the real script via subprocess from a
throwaway temp project and asserts the secret canary bytes never appear under
.tpr/targets/. It covers three escape vectors:

  V1  direct outside absolute path:  --target <outside-secret-file>
  V2  symlink-to-outside file root:  --target <proj/link -> outside-secret-file>
  V3  symlinked directory root:      --target <proj/link -> outside-secret-dir>

On HEAD (17dd045) all three vectors breach the boundary, so this test FAILS.
It is hermetic (builds its own fixtures), re-runnable, and self-cleaning. It
depends on nothing but the stdlib; run it with `python3 <thisfile>`. Exit 0 =
all vectors contained (pass); exit non-zero = a breach was demonstrated (fail).
"""
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prepare_review.py",
)

CANARY = "TOP-SECRET-CANARY-DO-NOT-EXFILTRATE-7f3a9b"


def _run(proj, target):
    """Run prepare_review.py --only --target <target> with cwd=proj."""
    return subprocess.run(
        [sys.executable, SCRIPT, "--target", target, "--only"],
        cwd=proj, capture_output=True, text=True,
    )


def _canary_leaked(proj):
    """True if the canary bytes appear in any file under proj/.tpr/targets/."""
    targets_dir = os.path.join(proj, ".tpr", "targets")
    if not os.path.isdir(targets_dir):
        return False, []
    leaked = []
    for dp, _, fs in os.walk(targets_dir):
        for f in fs:
            p = os.path.join(dp, f)
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    if CANARY in fh.read():
                        leaked.append(p)
            except OSError:
                pass
    return bool(leaked), leaked


def _vector(name, make_target):
    """Build a hermetic fixture, run the script, report (breached, detail).

    make_target(proj, outside) -> the string passed to --target. It may create
    a symlink inside `proj` pointing into `outside`.
    """
    root = tempfile.mkdtemp(prefix="tpr-containment-")
    try:
        proj = os.path.join(root, "proj")
        outside = os.path.join(root, "outside")
        os.makedirs(proj)
        os.makedirs(outside)
        target = make_target(proj, outside)
        cp = _run(proj, target)
        leaked, files = _canary_leaked(proj)
        rel = [os.path.relpath(f, proj) for f in files]
        detail = (f"exit={cp.returncode} "
                  f"stdout={cp.stdout.strip()!r} stderr={cp.stderr.strip()!r} "
                  f"leaked_files={rel}")
        return leaked, detail
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --- the three escape vectors -------------------------------------------------

def v1_direct_outside_path(proj, outside):
    secret = os.path.join(outside, "secret.txt")
    with open(secret, "w", encoding="utf-8") as fh:
        fh.write(CANARY + "\n")
    return secret                      # absolute path outside the project cwd


def v2_symlink_to_outside_file(proj, outside):
    secret = os.path.join(outside, "secret.txt")
    with open(secret, "w", encoding="utf-8") as fh:
        fh.write(CANARY + "\n")
    link = os.path.join(proj, "innocent.md")
    os.symlink(secret, link)           # symlink lives inside proj; resolves out
    return link


def v3_symlinked_dir_root(proj, outside):
    secretdir = os.path.join(outside, "secretdir")
    os.makedirs(secretdir)
    with open(os.path.join(secretdir, "leak.txt"), "w", encoding="utf-8") as fh:
        fh.write(CANARY + "\n")
    link = os.path.join(proj, "linkeddir")
    os.symlink(secretdir, link)        # symlinked directory ROOT inside proj
    return link


def main():
    if not os.path.isfile(SCRIPT):
        print(f"FAIL: prepare_review.py not found at {SCRIPT}")
        return 2

    vectors = [
        ("V1 direct-outside-path", v1_direct_outside_path),
        ("V2 symlink-to-outside-file", v2_symlink_to_outside_file),
        ("V3 symlinked-dir-root", v3_symlinked_dir_root),
    ]

    breaches = []
    for name, fn in vectors:
        breached, detail = _vector(name, fn)
        status = "BREACH" if breached else "contained"
        print(f"[{status}] {name}: {detail}")
        if breached:
            breaches.append(name)

    print()
    if breaches:
        print("FAIL: containment boundary breached — out-of-project bytes "
              f"landed under .tpr/targets/ for: {', '.join(breaches)}. "
              "copy_targets() must reject targets whose realpath is not under "
              "cwd (covering direct paths AND dereferenced symlink roots).")
        return 1

    print("PASS: all vectors contained — no out-of-project bytes reached "
          ".tpr/targets/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
