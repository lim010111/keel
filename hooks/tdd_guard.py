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
import re
import signal
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "hooks" / ".tdd-state"

CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h",
    ".cpp", ".cc", ".hpp", ".cs", ".swift", ".scala", ".ex", ".exs",
}


def read_input():
    """Read hook JSON from stdin with a timeout guard against hangs."""
    def _timeout(_signum, _frame):
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


def is_test_file(path):
    """Heuristic: is `path` a test/spec file (vs implementation)?

    Kept identical to tdd_mark.is_test_file — duplicated rather than shared so
    each hook stays a dependency-free standalone script.
    """
    norm = path.replace("\\", "/").lower()
    base = os.path.basename(norm)
    if re.search(r"\.(test|spec)\.", base):
        return True
    if re.search(r"(^|[_-])(test|spec)s?([_-]|\.)", base):
        return True
    parts = norm.split("/")
    return any(p in ("test", "tests", "spec", "specs", "__tests__", "__test__")
               for p in parts)


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
    payload = read_input()
    session_id = str(payload.get("session_id", "")) or "default"

    # TDD MODE off -> this guard is a no-op.
    if not (STATE_DIR / f"mode-{session_id}").exists():
        sys.exit(0)

    tool = str(payload.get("tool_name", ""))
    file_path = str(payload.get("tool_input", {}).get("file_path", ""))
    if not file_path:
        sys.exit(0)

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in CODE_EXT:
        sys.exit(0)            # docs / config — not guarded
    if is_test_file(file_path):
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
        sys.exit(0)            # a test exists this session -> guard satisfied

    name = os.path.basename(file_path)
    is_new_file = tool == "Write" and not os.path.exists(file_path)

    if is_new_file:
        print(BLOCK_MSG.format(name=name), file=sys.stderr)
        sys.exit(2)            # hard block: new impl file, zero tests

    print(WARN_MSG.format(name=name))   # existing file -> non-blocking advisory
    sys.exit(0)


if __name__ == "__main__":
    main()
