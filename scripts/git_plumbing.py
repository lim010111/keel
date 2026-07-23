#!/usr/bin/env python3
"""git_plumbing — shared never-raise git wrappers.

The one owner of the git subprocess discipline the harness scripts share:
UTF-8 decode with errors='replace', raw-bytes variant for binary-capable
output (diffs hashed canonically, blobs scanned at the call site), and
fail-soft returns (a broken cwd or missing git yields a nonzero rc, never an
exception). merge_gate_local.py and leak_scan.py used to carry these five as
verbatim twins ("reused verbatim from merge_gate_local.py"); a fix here now
lands in both. Merge-gate-specific plumbing (base resolution, review-tree
building) stays in merge_gate_local — only what both consumers share lives
here.
"""
import os
import subprocess
from pathlib import Path


def git(cwd, args, env=None):
    """Run git, returning (returncode, stdout_text). Never raises."""
    full_env = {**os.environ, **(env or {})}
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), env=full_env,
                           capture_output=True)
    except Exception as e:
        return 1, f"{e}"
    return p.returncode, p.stdout.decode("utf-8", "replace")


def git_bytes(cwd, args, env=None) -> tuple[int, bytes]:
    """Run git, returning (returncode, stdout_bytes). For output that may be
    binary — diff bytes (the canonical review hash is over them) and blob
    contents (decoded at the scan site with errors='replace')."""
    full_env = {**os.environ, **(env or {})}
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), env=full_env,
                           capture_output=True)
    except Exception:
        return 1, b""
    return p.returncode, p.stdout


def repo_root(cwd) -> Path | None:
    rc, out = git(cwd, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        return None
    return Path(out.strip())


def rev_parse(cwd, ref) -> str | None:
    rc, out = git(cwd, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if rc != 0:
        return None
    return out.strip() or None


def tip_bypass_reason(cwd, tip_sha: str, trailer: str) -> str | None:
    """Return the non-empty bypass reason from the tip commit's
    `<trailer>: <reason>` trailer, or None. Honored ONLY under
    client-side-blocking (D6)."""
    fmt = "--format=%(trailers:key=" + trailer + ",valueonly)"
    rc, out = git(cwd, ["log", "-1", fmt, tip_sha])
    if rc == 0 and out.strip():
        return out.strip()
    # Fallback: parse the raw body for older git without %(trailers) key filter.
    rc, body = git(cwd, ["log", "-1", "--format=%B", tip_sha])
    if rc != 0:
        return None
    prefix = f"{trailer}:"
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            reason = line[len(prefix):].strip()
            if reason:
                return reason
    return None
