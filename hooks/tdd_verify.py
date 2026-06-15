#!/usr/bin/env python3
"""TDD hook 3/4 - Stop verifier.

Fires every time Claude finishes responding. When code changed this turn
(per-session marker present), it detects the project's test command, runs
it, and hard-blocks the turn (exit 2) if the suite is not green.

Loop guard: enforces at most once per turn via `stop_hook_active`.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

MARKER_DIR = Path.home() / ".claude" / "hooks" / ".tdd-markers"

# Upper bound on a single suite's wall time. tdd_verify is the sole Stop-time
# oracle (ADR-0022), and a hung suite (waits on a port, an interactive prompt, an
# infinite loop) would otherwise freeze turn completion — worse now that the venue
# fix can run several repos' suites in one Stop. A timeout is a non-blocking infra
# skip, never a red verdict (Stop-time private feedback). Env-overridable for tests.
ORACLE_TIMEOUT_SECONDS = int(os.environ.get("TDD_ORACLE_TIMEOUT_SECONDS", "600"))


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


def find_up(start, names):
    """Walk up from `start` to find the first dir containing any of `names`.

    Returns (directory, matched_name) or (None, None). Stops at the
    filesystem root and does not ascend above the user's home directory.
    """
    home = Path.home()
    cur = Path(start).resolve()
    while True:
        for name in names:
            if (cur / name).exists():
                return cur, name
        if cur == cur.parent or cur == home:
            return None, None
        cur = cur.parent


def pytest_available():
    """True if pytest can be imported by python3 in this environment."""
    try:
        return subprocess.run(
            ["python3", "-c", "import pytest"],
            capture_output=True, timeout=15,
        ).returncode == 0
    except Exception:
        return False


def detect_test_command(start):
    """Return (project_root, command) for the test suite rooted at or above
    `start`, or (None, None). The command MUST be run with cwd=project_root.

    Priority: an explicit `.claude/tdd-test-cmd` override file, then
    conventional ecosystem detection. Auto-detected commands are returned
    only when their runner is actually installed -> a missing runner is an
    infra gap, not a test failure, and must not block the turn.
    """
    # 1. per-project override file (first non-empty, non-comment line).
    #    The user set this explicitly, so it is trusted and returned as-is.
    ovr_dir, _ = find_up(start, [".claude/tdd-test-cmd"])
    if ovr_dir:
        try:
            for line in (ovr_dir / ".claude" / "tdd-test-cmd").read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return ovr_dir, line
        except Exception:
            pass

    # 2. conventional detection by project marker file
    root, marker = find_up(
        start, ["package.json", "pyproject.toml", "pytest.ini", "Cargo.toml", "go.mod"]
    )
    if not root:
        return None, None

    if marker == "package.json":
        try:
            pkg = json.loads((root / "package.json").read_text())
        except Exception:
            return None, None
        if "test" not in pkg.get("scripts", {}):
            return None, None  # no test script -> nothing to enforce
        if (root / "pnpm-lock.yaml").exists():
            pm = "pnpm"
        elif (root / "yarn.lock").exists():
            pm = "yarn"
        elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            pm = "bun"
        else:
            pm = "npm"
        if not shutil.which(pm):
            return None, None
        return root, (f"{pm} run test" if pm == "bun" else f"{pm} test")

    if marker in ("pyproject.toml", "pytest.ini"):
        return (root, "python3 -m pytest -q") if pytest_available() else (None, None)

    if marker == "Cargo.toml":
        return (root, "cargo test") if shutil.which("cargo") else (None, None)

    if marker == "go.mod":
        return (root, "go test ./...") if shutil.which("go") else (None, None)

    return None, None


def _run_suite(cmd, root):
    """Run `cmd` in `root` with a bounded timeout and a process-group kill.

    Returns (returncode, combined_output), or None when the suite could not be
    launched OR timed out — both are non-blocking infra skips (a hung or
    unrunnable suite must never freeze the Stop hook; this is private feedback).
    `start_new_session=True` puts the shell and its children in their own process
    group, so a timeout kill (`killpg`) reaps the whole tree, not just the shell.
    """
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        return None
    try:
        out, _ = proc.communicate(timeout=ORACLE_TIMEOUT_SECONDS)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return None


def main():
    payload = read_input()
    session_id = str(payload.get("session_id", "")) or "default"
    cwd = payload.get("cwd") or os.getcwd()

    # loop guard: this Stop was caused by our own block -> enforce only once
    if payload.get("stop_hook_active") is True:
        sys.exit(0)

    marker = MARKER_DIR / f"marker-{session_id}"
    if not marker.exists():
        sys.exit(0)  # no code changed this turn
    try:
        edited = [p.strip() for p in marker.read_text().splitlines() if p.strip()]
    except Exception:
        edited = []
    try:
        marker.unlink()
    except Exception:
        pass

    # Venue: resolve the oracle by the repo whose files CHANGED this turn (the
    # paths tdd_mark records in the marker), NOT the session cwd. Under the
    # operator's plan-repo-session / code-repo-edit workflow these differ, so
    # cwd resolves the wrong repo and the verifier silently no-ops on code-repo
    # edits (ADR-0023; same venue class as ADR-0014). Working-tree timing is
    # correct here — this is Stop-time *private feedback*, not a committed-tip
    # attestation, so ADR-0014's reason for rejecting file-path resolution does
    # not apply. An empty/legacy marker degrades to the session cwd.
    start_dirs = [str(Path(p).resolve().parent) for p in edited] or [cwd]

    # Dedup by resolved project root: many edited files in one repo -> one run;
    # files spanning repos -> one run per repo (each changed repo is verified).
    targets = {}
    for start in start_dirs:
        root, cmd = detect_test_command(start)
        if root and cmd:
            targets[str(root)] = cmd
    if not targets:
        sys.exit(0)  # no recognizable oracle in any edited repo -> skip silently

    failures = []
    for root, cmd in targets.items():
        res = _run_suite(cmd, root)
        if res is None:
            # unrunnable or timed out -> infra skip, never block (private feedback)
            print(f"TDD hook: test suite in {root} did not complete "
                  f"(unrunnable or exceeded {ORACLE_TIMEOUT_SECONDS}s) — skipped.",
                  file=sys.stderr)
            continue
        rc, output = res
        # pytest exit 5 == "no tests collected" -> treat as non-blocking
        if rc == 0 or (rc == 5 and "pytest" in cmd):
            continue
        tail = output.strip().splitlines()[-40:]
        failures.append((root, cmd, rc, tail))

    if not failures:
        sys.exit(0)

    blocks = [
        f"--- {root}: `{cmd}` (exit {rc}) ---\n" + "\n".join(tail)
        for root, cmd, rc, tail in failures
    ]
    msg = (
        f"Tests are NOT green. You changed code this turn but the oracle failed "
        f"in {len(failures)} repo(s). Reach GREEN before finishing this turn — "
        f"follow the tdd skill: minimal code to pass, then re-run.\n\n"
        + "\n\n".join(blocks)
    )
    print(msg, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
