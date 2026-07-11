#!/usr/bin/env python3
"""Shared fail-open JSONL journal for the global hooks (harness-journal#01).

claude-harness-work harness-journal#01. When this lands in claude-config,
reference it in commits as `refs harness-journal#01`.

Hooks talk to stderr and vanish — there was no record of which hook fired,
blocked, passed, or skipped, and why (2026-07-11 dual harness review). Each
integrated hook appends one lifecycle event per invocation, so questions like
"narrative_guard 오차단률" or "tdd_verify skip 사유 분포" are answered from
data instead of one-off measurement campaigns.

Journal:   ~/.claude/hooks/.hook-journal.jsonl   (override: HOOK_JOURNAL_PATH)
Rotation:  when the file reaches HOOK_JOURNAL_MAX_BYTES (default 5 MB) it is
           renamed to <path>.1 (replacing the previous .1) and a fresh file
           starts — the .sound-debug.log self-trim precedent, generalized to
           an O(1) size check per append instead of a line-count trim.
Kill:      HOOK_JOURNAL_DISABLED=1 no-ops every write. The hook test suites
           set it so hook subprocesses under test never spam the real journal.

Event schema — one JSON object per line, keys kept minimal until consumers
(counter reports, dashboards) exist:
    ts          local ISO-8601 timestamp with UTC offset
    hook        emitting hook ("tdd_verify", "narrative_guard", "status", ...)
    event       fire | block | pass | skip | drift
    reason      short machine-greppable code (":"-namespaced where useful)
    session_id  from the hook payload ("" when the caller has no payload)
    repo        repo root or payload cwd ("" when unknown)

Event meanings: `fire` = the hook DID its thing (armed, recorded, played,
anchored, regenerated); `block` = it stopped the turn/tool (exit 2); `pass` =
it evaluated and let things through; `skip` = nothing to do, or failed open;
`drift` = the payload-contract counter below. Per-hook one-line semantics
live in ~/.claude/README.md § Hook journal.

Payload-contract drift (the 974a review C3 class — a Claude Code version bump
silently neutering hooks by renaming/dropping stdin fields): check_payload()
journals a `drift` event naming the expected keys ABSENT from the payload.
Key absence only — a present-but-empty value is not drift.

Contract for callers: nothing here ever raises, prints, or blocks — a journal
failure (disk, permission, bug) must never change a hook's real behaviour.
Appends are single small O_APPEND writes (atomic in practice); a concurrent
double-rotation can drop a line, which an advisory journal accepts.

Integration snippet (guarded import — the hook degrades to a standalone,
journal-free script when this module is missing or broken):

    try:
        sys.path.append(str(Path.home() / ".claude" / "scripts"))
        from hook_journal import for_hook as _for_hook
        _journal, _drift = _for_hook("my_hook")
    except Exception:
        _journal = _drift = lambda *a, **k: None
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = Path.home() / ".claude" / "hooks" / ".hook-journal.jsonl"
DEFAULT_MAX_BYTES = 5_000_000  # ~30k events; rotated once to <path>.1


def journal_path() -> Path:
    return Path(os.environ.get("HOOK_JOURNAL_PATH") or DEFAULT_PATH)


def _max_bytes() -> int:
    try:
        return int(os.environ.get("HOOK_JOURNAL_MAX_BYTES") or DEFAULT_MAX_BYTES)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES


def journal(hook, event, reason="", payload=None, repo=None):
    """Append one event line. NEVER raises and never writes to stdout/stderr —
    hook stdout/exit codes are semantic, so the journal must stay invisible."""
    try:
        if os.environ.get("HOOK_JOURNAL_DISABLED") == "1":
            return
        if not isinstance(payload, dict):
            payload = {}
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "hook": str(hook),
            "event": str(event),
            "reason": str(reason),
            "session_id": str(payload.get("session_id") or ""),
            "repo": str(repo if repo is not None else payload.get("cwd") or ""),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:  # bounded: rotate once the file reaches the cap
            if path.stat().st_size >= _max_bytes():
                os.replace(path, str(path) + ".1")
        except OSError:
            pass  # no file yet, or the rotate raced/failed: append anyway
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass  # fail-open: a journal failure must never break the hook


def check_payload(hook, payload, expected):
    """Payload-contract drift counter: journal one `drift` event when any
    `expected` KEY is absent from the hook's stdin payload. Returns the list
    of missing keys (callers may ignore it). Never raises."""
    try:
        if not isinstance(payload, dict):
            payload = {}
        missing = [k for k in expected if k not in payload]
        if missing:
            journal(hook, "drift", "missing:" + ",".join(missing), payload=payload)
        return missing
    except Exception:
        return []


def for_hook(hook):
    """(journal, check_payload) bound to one hook name — the pair the guarded
    integration snippet unpacks. Both callables inherit never-raise."""
    def _journal(event, reason="", payload=None, repo=None):
        journal(hook, event, reason, payload=payload, repo=repo)

    def _drift(payload, expected):
        return check_payload(hook, payload, expected)

    return _journal, _drift


def _main(argv):
    """CLI for shell hooks that have no Python side:
        hook_journal.py <hook> <event> [reason] [expected-fields-csv]
    with the hook payload JSON piped on stdin (optional). Runs the drift
    counter when expected fields are given, then journals the event. Used by
    scripts/sound_permission.sh. Always exits 0."""
    hook = argv[1] if len(argv) > 1 else "unknown"
    event = argv[2] if len(argv) > 2 else "fire"
    reason = argv[3] if len(argv) > 3 else ""
    expected = [f for f in (argv[4].split(",") if len(argv) > 4 else []) if f]
    payload = {}
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if expected:
        check_payload(hook, payload, expected)
    journal(hook, event, reason, payload=payload)


if __name__ == "__main__":
    try:
        _main(sys.argv)
    except Exception:
        pass
    sys.exit(0)
