#!/usr/bin/env python3
"""merge-gate hook 2/2 — Stop cheap scheduler (claude-harness-work #30 D2).

Stop fires on every turn, so running the expensive `produce` on each Stop is
wrong. This hook is a CHEAP, NON-BLOCKING scheduler: it enqueues a background
`merge-gate-local produce` only when a set of cheap guards ALL hold, and it
NEVER blocks the turn (always exits 0 — unlike a blocking review gate, which is
the codex-plugin-cc stop-review anti-pattern #30 D2 calls out).

Guards (the authoritative list is #30 § Scheduler & hooks):
  * profile == local         (self-gate; no-op in any other repo)
  * auto_produce == stop-debounced
  * recursion guard MERGE_GATE_PRODUCER_RUNNING != 1
  * dirty marker present
  * debounce elapsed since last edit, min-interval elapsed since last produce
  * non-empty in-scope diff
  * diff_hash != last reviewed/running   (same diff repeated = no-op)
  * no producer lock held

The expensive canonical diff is computed ONLY after the cheap guards pass.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path.home() / ".claude" / "scripts"
WRAPPER = SCRIPTS / "merge_gate_local.py"
sys.path.insert(0, str(SCRIPTS))

try:
    import merge_gate_local as mg
except Exception:
    mg = None

STATE_ROOT = Path(os.environ.get("MERGE_GATE_STATE_ROOT",
                                 str(Path.home() / ".claude" / "hooks" / ".merge-gate-state")))


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


def repo_state_dir(root: Path) -> Path:
    import hashlib
    h = hashlib.sha1(str(root).encode()).hexdigest()[:16]
    return STATE_ROOT / h


def load_state(sdir: Path) -> dict:
    f = sdir / "state.json"
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def save_state(sdir: Path, state: dict) -> None:
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "state.json").write_text(json.dumps(state))
    except Exception:
        pass


def cheap_gate(state: dict, cfg, now: float, producer_running: bool) -> tuple[bool, str]:
    """Cheap guards that need no git work. Returns (ok_to_continue, reason)."""
    sched = cfg.scheduler
    if sched.get("auto_produce") == "off":
        return False, "auto_produce off"
    if producer_running:
        return False, "recursion guard (MERGE_GATE_PRODUCER_RUNNING=1)"
    if not state.get("dirty"):
        return False, "not dirty"
    if now - float(state.get("last_edit_ts", 0)) < float(sched.get("debounce_seconds", 90)):
        return False, "debouncing (recent edit)"
    if now - float(state.get("last_produce_ts", 0)) < float(sched.get("min_interval_seconds", 600)):
        return False, "min-interval not elapsed"
    return True, "cheap guards passed"


def diff_gate(state: dict, cd: dict, lock_held: bool) -> tuple[bool, str]:
    """Guards needing the canonical diff. Returns (ok_to_produce, reason)."""
    if not cd["changed_files"]:
        return False, "no in-scope changes"
    if cd["diff_hash"] == state.get("last_diff_hash"):
        return False, "same diff already reviewed/running"
    if lock_held:
        return False, "producer lock held"
    return True, "produce"


def _lock_held(root: Path, cfg) -> bool:
    lock = root / cfg.artifact_root / ".producer.lock"
    if not lock.exists():
        return False
    try:
        ts = int(lock.read_text().split()[1])
        return (time.time() - ts) <= mg.LOCK_TTL_SECONDS
    except Exception:
        return True


def main():
    if mg is None:
        sys.exit(0)
    payload = read_input()
    if payload.get("stop_hook_active") is True:
        sys.exit(0)  # our own re-entry — never loop
    cwd = payload.get("cwd") or os.getcwd()
    root = mg.repo_root(Path(cwd))
    if root is None:
        sys.exit(0)
    if not mg.is_local_profile(root):
        sys.exit(0)  # self-gate — explicit opt-in only
    cfg = mg.load_config(root)

    producer_running = os.environ.get("MERGE_GATE_PRODUCER_RUNNING") == "1"
    sdir = repo_state_dir(root)
    state = load_state(sdir)
    now = time.time()

    ok, reason = cheap_gate(state, cfg, now, producer_running)
    if not ok:
        sys.exit(0)

    base = mg.resolve_base_sha(root, cfg.base_ref)
    if base is None:
        sys.exit(0)
    cd = mg.canonical_diff(root, base, cfg.review_globs, cfg.ignore_globs)
    ok, reason = diff_gate(state, cd, _lock_held(root, cfg))
    if not ok:
        sys.exit(0)

    # Launch produce in the background — non-blocking. Record the dedup keys
    # immediately so a repeated Stop on the same diff is a no-op while it runs.
    state["last_produce_ts"] = now
    state["last_diff_hash"] = cd["diff_hash"]
    state["dirty"] = False
    save_state(sdir, state)
    launch_produce(root, sdir)
    sys.exit(0)


def launch_produce(root: Path, sdir: Path) -> None:
    """Fire-and-forget a background `produce`. Non-blocking; failures are
    swallowed (the scheduler must never block the turn). NB: produce runs
    WITHOUT MERGE_GATE_PRODUCER_RUNNING in its own env — that guard is set only
    on the validator's `claude -p` child (default_validator_runner), so the
    nested session's Stop hook no-ops while THIS produce runs normally."""
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        lf = open(sdir / "produce.log", "ab")
        subprocess.Popen(
            [sys.executable, str(WRAPPER), "--cwd", str(root), "produce"],
            cwd=str(root),
            env={k: v for k, v in os.environ.items()
                 if k != "MERGE_GATE_PRODUCER_RUNNING"},
            stdout=lf, stderr=lf, start_new_session=True,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
