#!/usr/bin/env python3
"""TDD hook 2/4 - PostToolUse(Edit|Write) change marker.

Two jobs, both per-session:
  1. Drop a marker file whenever Claude edits a source/test file, so the
     Stop hook (tdd_verify.py) only runs the suite on turns that touched code.
  2. While TDD MODE is on, record the wall-clock time of the last *test*
     edit and the last *implementation* edit. The PreToolUse guard
     (tdd_guard.py) reads this to enforce test-first ordering.

Docs/config edits (.md/.json/.yaml/...) are deliberately excluded — they are
neither code-under-test nor tests.
"""
import json
import sys
import time
from pathlib import Path

import hook_io
import tdd_paths

MARKER_DIR = Path.home() / ".claude" / "hooks" / ".tdd-markers"
STATE_DIR = Path.home() / ".claude" / "hooks" / ".tdd-state"

# Journal (harness-journal#01): observability is strictly additive; hook_io
# hands back never-raise no-op fallbacks on any failure.
_journal, _drift = hook_io.journal_for("tdd_mark")


def main():
    payload = hook_io.read_payload()
    _drift(payload, ("session_id", "cwd", "tool_name", "tool_input"))
    session_id = hook_io.session_id(payload)
    file_path = str(payload.get("tool_input", {}).get("file_path", ""))
    if not file_path or not tdd_paths.is_code_file(file_path):
        _journal("skip", "non-code", payload)
        sys.exit(0)

    # 1. Stop-hook marker — record that code changed this turn, accumulating the
    #    DISTINCT edited file paths (newline-separated). The Stop verifier
    #    (tdd_verify.py) resolves the oracle for EVERY repo touched this turn from
    #    these paths, keyed off the marker rather than the session cwd — the venue
    #    fix (ADR-0023): cwd is the wrong repo under the plan-repo-session /
    #    code-repo-edit workflow.
    try:
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        mf = MARKER_DIR / f"marker-{session_id}"
        try:
            existing = mf.read_text().splitlines() if mf.exists() else []
        except Exception:
            existing = []
        if file_path not in existing:
            existing.append(file_path)
            mf.write_text("\n".join(existing) + "\n")
    except Exception:
        pass  # never block a tool call over a marker write

    # 2. test-first ordering state — only while TDD MODE is on.
    if (STATE_DIR / f"mode-{session_id}").exists():
        edits = STATE_DIR / f"edits-{session_id}.json"
        try:
            data = json.loads(edits.read_text())
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data["last_test" if tdd_paths.is_test_file(file_path) else "last_impl"] = time.time()
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            edits.write_text(json.dumps(data))
        except Exception:
            pass

    _journal("fire", "marker-recorded", payload)
    sys.exit(0)


if __name__ == "__main__":
    main()
