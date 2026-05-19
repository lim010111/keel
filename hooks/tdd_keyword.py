#!/usr/bin/env python3
"""TDD hook 1/4 - UserPromptSubmit trigger + sticky mode.

Turns TDD MODE on when the user's prompt signals test-driven intent, then
keeps it on for the rest of the session (sticky) until an explicit off
phrase. While active, the `[TDD MODE]` pointer is re-injected on *every*
prompt — so the steering survives follow-up prompts that never repeat the
keyword (the failure mode that let an earlier feature skip TDD entirely).

Sticky state lives in ~/.claude/hooks/.tdd-state/mode-<session_id>; the
PreToolUse guard (tdd_guard.py) and the edit marker (tdd_mark.py) read it.
"""
import json
import re
import signal
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / "hooks" / ".tdd-state"


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


POINTER = """\
[TDD MODE]
The user invoked TDD. Use the `tdd` skill for this task and follow it strictly:
- Test-FIRST: write one failing test, then minimal code to pass it (red -> green).
- Vertical slices via tracer bullets: one test -> one implementation -> repeat.
  Never write all tests first (no horizontal slicing).
- Tests verify behavior through public interfaces, not implementation details.
- Never refactor while red. Reach green first.
TDD MODE is STICKY for this whole session — it stays on across follow-up
prompts even when they do not mention "tdd". A PreToolUse guard hard-blocks
creating a new implementation file before any test file is touched; the Stop
hook blocks a non-green suite. Load the skill now for the full workflow.
To leave TDD MODE the user says e.g. "tdd off" / "tdd 종료"."""

OFF_NOTE = (
    "[TDD MODE OFF] TDD MODE has been turned off for the rest of this session. "
    "Test-first is no longer enforced."
)

# Explicit off switch — checked BEFORE the on-triggers so "tdd off" (which
# also contains "tdd") disarms instead of re-arming.
OFF_RE = re.compile(
    r"(?<![a-zA-Z])tdd(?![a-zA-Z])\s*(?:off|stop|disable|끄|꺼|종료|해제|그만|중단|중지|해줘?\s*그만)"
    r"|(?:no|stop|disable|turn\s*off|exit)\s+tdd(?![a-zA-Z])"
    r"|tdd\s*모드?\s*(?:종료|해제|끄|꺼|그만|중단|off)",
    re.IGNORECASE,
)

# On switches — genuine test-driven intent only. Bare "test" / "테스트" is
# deliberately NOT a trigger: it appears in every "run the tests" prompt and
# would false-activate the guard. Once any of these fires, sticky state keeps
# the mode on regardless of later wording.
ON_RES = [
    re.compile(r"(?<![a-zA-Z])tdd(?![a-zA-Z])", re.IGNORECASE),
    re.compile(r"test[\s-]*driven", re.IGNORECASE),
    re.compile(r"test[\s-]*first", re.IGNORECASE),
    re.compile(r"red[\s-]*green", re.IGNORECASE),
    re.compile(r"테스트\s*주도"),
    re.compile(r"테스트\s*(?:먼저|우선|부터)"),
    re.compile(r"레드[\s-]*그린"),
]


def main():
    payload = read_input()
    prompt = str(payload.get("prompt", ""))
    session_id = str(payload.get("session_id", "")) or "default"
    state = STATE_DIR / f"mode-{session_id}"

    # off switch wins outright — disarm and stop.
    if OFF_RE.search(prompt):
        try:
            if state.exists():
                state.unlink()
        except Exception:
            pass
        print(OFF_NOTE)
        sys.exit(0)

    activated = any(r.search(prompt) for r in ON_RES)
    if activated:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            state.write_text(str(time.time()))
        except Exception:
            pass  # never block a prompt over a state write

    # sticky: re-inject the pointer on every prompt while the mode file exists.
    if activated or state.exists():
        print(POINTER)
    sys.exit(0)


if __name__ == "__main__":
    main()
