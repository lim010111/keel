#!/usr/bin/env python3
"""merge-gate hook 1/2 — PostToolUse(Edit|Write) dirty marker (#30 D2).

Records that code changed this turn in a per-repo state file, so the Stop
scheduler (merge_gate_scheduler.py) only considers `produce` on turns that
actually touched the repo. Self-gates: a no-op unless the cwd repo's
harness.toml selects the local merge-gate profile. Models the tdd_mark.py
marker pattern; never blocks a tool call (always exits 0)."""
import json
import os
import signal
import sys
import time
from pathlib import Path

SCRIPTS = Path.home() / ".claude" / "scripts"
sys.path.insert(0, str(SCRIPTS))

try:
    import merge_gate_local as mg
except Exception:
    mg = None

# Same per-repo state layout the scheduler reads.
from merge_gate_scheduler import repo_state_dir, load_state, save_state  # noqa: E402


def read_input():
    def _timeout(_s, _f):
        raise TimeoutError
    data = ""
    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(5)
        data = sys.stdin.read()
        signal.alarm(0)
    except Exception:
        data = ""
    try:
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def main():
    if mg is None:
        sys.exit(0)
    payload = read_input()
    cwd = payload.get("cwd") or os.getcwd()
    file_path = str(payload.get("tool_input", {}).get("file_path", ""))
    root = mg.repo_root(Path(cwd))
    if root is None:
        sys.exit(0)
    if not mg.is_local_profile(root):
        sys.exit(0)  # self-gate — explicit opt-in only
    cfg = mg.load_config(root)

    # Only mark when the edited file is in review scope — an edit to an
    # ignored path (e.g. the artefact cache) should not schedule a review.
    if file_path:
        try:
            rel = os.path.relpath(file_path, str(root))
        except Exception:
            rel = file_path
        rel = rel.replace("\\", "/")
        if not rel.startswith("..") and not mg.in_scope(rel, cfg.review_globs, cfg.ignore_globs):
            sys.exit(0)

    sdir = repo_state_dir(root)
    state = load_state(sdir)
    state["dirty"] = True
    state["last_edit_ts"] = time.time()
    save_state(sdir, state)
    sys.exit(0)


if __name__ == "__main__":
    main()
