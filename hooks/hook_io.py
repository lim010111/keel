#!/usr/bin/env python3
"""hook_io — the shared hook-protocol I/O seam for ~/.claude/hooks.

Every Claude Code hook crosses the same protocol interface: a JSON payload on
stdin (which may hang, arrive torn, or be empty), a session id and cwd inside
it (both optional), the Stop-event loop-guard flag, and an optional journal
(hook_journal, strictly additive). Before this module each hook re-implemented
that preamble verbatim; the copies could drift and the edge cases (timeout,
torn JSON) were only ever exercised implicitly through six subprocess suites.

Interface (all a hook needs to know):
    read_payload()          -> dict   stdin JSON; {} on timeout/torn/empty
    session_id(payload)     -> str    "default" when absent
    cwd(payload)            -> str    process cwd when absent
    is_reentrant_stop(p)    -> bool   Stop fired by our own block (enforce once)
    journal_for(hook_name)  -> (journal, drift)  no-op pair on any failure

Hard import by design: hooks that read stdin through this module depend on it
the way they already depend on Python itself. (The retired
merge_gate_scheduler.py predates this module and keeps its own copy — its
arrangement is ADR-0014-recorded and its tests monkeypatch that copy.)
"""
import json
import os
import signal
import sys
from pathlib import Path


def read_payload():
    """Read the hook JSON payload from stdin with a hang guard.

    5s SIGALRM cap; any failure (timeout, unreadable stdin, torn or empty
    JSON) degrades to {} — a hook must never hang or crash on protocol I/O.
    """
    def _timeout(_signum, _frame):
        raise TimeoutError

    data = ""
    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(5)
        try:
            data = sys.stdin.read()
        finally:
            # ALWAYS cancel — a read() that raises anything but the alarm's own
            # TimeoutError (UnicodeDecodeError on a non-UTF-8 payload, OSError on
            # an odd fd) used to leave the alarm pending, so TimeoutError fired
            # later inside the hook's real work: mid tdd_verify oracle run or mid
            # narrative_guard git call, crashing a Stop hook that fails OPEN.
            signal.alarm(0)
    except Exception:
        data = ""
    try:
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def session_id(payload):
    return str(payload.get("session_id", "")) or "default"


def cwd(payload):
    return payload.get("cwd") or os.getcwd()


def is_reentrant_stop(payload):
    """True when this Stop event was caused by a hook's own block — the
    caller enforces at most once per turn."""
    return payload.get("stop_hook_active") is True


def journal_for(hook_name):
    """Guarded journal handles (harness-journal#01): observability is strictly
    additive, so a missing or broken hook_journal must never change a hook's
    behaviour — on any failure both callables are never-raise no-ops."""
    try:
        sys.path.append(str(Path.home() / ".claude" / "scripts"))
        from hook_journal import for_hook
        return for_hook(hook_name)
    except Exception:
        noop = lambda *a, **k: None  # noqa: E731
        return noop, noop
