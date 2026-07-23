#!/usr/bin/env python3
"""TDD hook 4/4 - PreToolUse(Edit|Write) test-first guard.

Active only while TDD MODE is on (sticky session file from tdd_keyword.py).
Closes the hole that let an earlier feature be built implementation-first:
nothing used to stop Claude from writing all the code and only then the
tests. The Stop hook checks the suite is GREEN — but all-impl-then-all-tests
ends green too, so green alone never catches horizontal slicing.

Hybrid enforcement (the user's chosen policy). The trigger is simple and
low-false-positive: *no test file has been edited yet this session*.
  - Creating a NEW implementation file in that state -> hard block (exit 2).
    Write the failing test first.
  - Editing an EXISTING file in that state -> non-blocking advisory only
    (bugfix / refactor / reaching green legitimately edit existing code).
Once any test file is edited, the guard goes silent for the session.
Test files, docs, and config are never guarded.
"""
import json
import os
import sys
from pathlib import Path

import hook_io
import tdd_paths

STATE_DIR = Path.home() / ".claude" / "hooks" / ".tdd-state"

# Journal (harness-journal#01): observability is strictly additive; hook_io
# hands back never-raise no-op fallbacks on any failure.
_journal, _drift = hook_io.journal_for("tdd_guard")


BLOCK_MSG = """\
TDD MODE — blocked: creating a new implementation file ({name}) before any \
test exists this session.

You are about to write production code without a failing test driving it. \
That is exactly the horizontal-slicing mistake TDD MODE exists to prevent.

Do this instead:
1. Create the test file first and write ONE failing test (red).
2. Then write the minimal implementation to pass it (green).
3. One test -> one slice -> repeat. Never batch all tests at the end.

See the `tdd` skill for the full red-green-refactor workflow. (To leave \
TDD MODE the user says "tdd off".)"""

WARN_MSG = """\
[TDD MODE] You are editing an implementation file ({name}) but no test file \
has been touched this session. If this edit is new behavior, stop and write \
a failing test first (red -> green). If it is a bugfix or refactor, add/keep \
a test that proves it. See the `tdd` skill."""


def main():
    payload = hook_io.read_payload()
    _drift(payload, ("session_id", "cwd", "tool_name", "tool_input"))
    session_id = hook_io.session_id(payload)

    # TDD MODE off -> this guard is a no-op.
    if not (STATE_DIR / f"mode-{session_id}").exists():
        _journal("skip", "mode-off", payload)
        sys.exit(0)

    tool = str(payload.get("tool_name", ""))
    file_path = str(payload.get("tool_input", {}).get("file_path", ""))
    if not file_path:
        _journal("skip", "no-file-path", payload)
        sys.exit(0)

    if not tdd_paths.is_code_file(file_path):
        _journal("skip", "non-code", payload)
        sys.exit(0)            # docs / config — not guarded
    if tdd_paths.is_test_file(file_path):
        _journal("pass", "test-file", payload)
        sys.exit(0)            # editing tests is always allowed

    # Implementation file. Has a test file been edited this session yet?
    last_test = 0
    try:
        data = json.loads((STATE_DIR / f"edits-{session_id}.json").read_text())
        if isinstance(data, dict):
            last_test = data.get("last_test", 0) or 0
    except Exception:
        last_test = 0

    if last_test > 0:
        _journal("pass", "test-first-satisfied", payload)
        sys.exit(0)            # a test exists this session -> guard satisfied

    name = os.path.basename(file_path)
    is_new_file = tool == "Write" and not os.path.exists(file_path)

    if is_new_file:
        print(BLOCK_MSG.format(name=name), file=sys.stderr)
        _journal("block", "new-impl-before-test", payload)
        sys.exit(2)            # hard block: new impl file, zero tests

    print(WARN_MSG.format(name=name))   # existing file -> non-blocking advisory
    _journal("fire", "advisory-impl-edit", payload)
    sys.exit(0)


if __name__ == "__main__":
    main()
