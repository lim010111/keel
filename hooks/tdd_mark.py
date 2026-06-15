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
import os
import re
import signal
import sys
import time
from pathlib import Path

MARKER_DIR = Path.home() / ".claude" / "hooks" / ".tdd-markers"
STATE_DIR = Path.home() / ".claude" / "hooks" / ".tdd-state"

# Source/test file extensions worth re-running tests for.
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

    Recognizes `foo.test.ts` / `foo.spec.js`, `test_foo.py` / `foo_test.go`,
    and any path under a `test(s)` / `spec(s)` / `__tests__` directory.
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


def main():
    payload = read_input()
    session_id = str(payload.get("session_id", "")) or "default"
    file_path = str(payload.get("tool_input", {}).get("file_path", ""))
    if not file_path or os.path.splitext(file_path)[1].lower() not in CODE_EXT:
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
        data["last_test" if is_test_file(file_path) else "last_impl"] = time.time()
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            edits.write_text(json.dumps(data))
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
