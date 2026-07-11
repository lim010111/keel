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

# Journal (harness-journal#01): observability is strictly additive — a missing
# or broken helper must never change this hook's behaviour, so the import is
# guarded and both callables are never-raise no-op fallbacks on any failure.
try:
    sys.path.append(str(Path.home() / ".claude" / "scripts"))
    from hook_journal import for_hook as _for_hook
    _journal, _drift = _for_hook("tdd_keyword")
except Exception:
    _journal = _drift = lambda *a, **k: None


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

# work-interval-tdd#09: UserPromptSubmit also fires for machine-injected
# turns (background task-notifications, system notices) whose bodies can
# mention TDD — e.g. a background agent reporting on these very hooks. The
# hook payload carries no field that distinguishes typed input from injected
# text, so the rule is a TEXT HEURISTIC over markers observed live
# 2026-07-11, in two layers:
#
#   1. A prompt whose stripped body STARTS WITH a known injection header
#      ("[SYSTEM NOTIFICATION", "<task-notification>", or the harness's
#      other wrapper "<system-reminder>") is non-user text: it must neither
#      arm nor disarm the sticky mode (it still receives the pointer
#      re-injection while the mode is on).
#   2. A COMPLETE <task-notification>/<system-reminder>...</...> block
#      quoted anywhere inside a user prompt is machine speech, not the
#      user's — it is stripped before keyword matching, so a keyword that
#      appears only inside the quoted block does not arm.
#
# Boundary default: anything else — e.g. marker strings merely mentioned
# mid-text, or a truncated block with no closing tag — counts as USER text
# and DOES arm. Injected turns are mechanically generated with a fixed head
# caught by layer 1, so the residue is near-certainly human; we accept the
# rare false ARM there over silently ignoring a real invocation (미발 has no
# visible failure signal — the user believes the mode is on when it isn't).
NONUSER_PREFIXES = (
    "[system notification",
    "<task-notification",
    "<system-reminder",
)

INJECTED_BLOCK_RE = re.compile(
    r"<(task-notification|system-reminder)>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def is_nonuser_text(prompt):
    return prompt.strip().lower().startswith(NONUSER_PREFIXES)


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
    _drift(payload, ("session_id", "cwd", "prompt"))
    prompt = str(payload.get("prompt", ""))
    session_id = str(payload.get("session_id", "")) or "default"
    state = STATE_DIR / f"mode-{session_id}"

    # non-user text (work-interval-tdd#09): no arm, no disarm; sticky
    # re-injection still applies while the mode file exists.
    if is_nonuser_text(prompt):
        if state.exists():
            print(POINTER)
            _journal("pass", "nonuser-reinject", payload)
        else:
            _journal("skip", "nonuser", payload)
        sys.exit(0)

    # quoted machine blocks never count as the user's own speech (#09).
    prompt = INJECTED_BLOCK_RE.sub("", prompt)

    # off switch wins outright — disarm and stop.
    if OFF_RE.search(prompt):
        try:
            if state.exists():
                state.unlink()
        except Exception:
            pass
        print(OFF_NOTE)
        _journal("fire", "disarmed", payload)
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
        _journal("fire" if activated else "pass",
                 "armed" if activated else "sticky-reinject", payload)
    else:
        _journal("skip", "no-trigger", payload)
    sys.exit(0)


if __name__ == "__main__":
    main()
