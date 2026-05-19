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


def detect_test_command(cwd):
    """Return a shell command string for the project's test suite, or None.

    Priority: an explicit `.claude/tdd-test-cmd` override file, then
    conventional ecosystem detection. Auto-detected commands are returned
    only when their runner is actually installed -> a missing runner is an
    infra gap, not a test failure, and must not block the turn.
    """
    # 1. per-project override file (first non-empty, non-comment line).
    #    The user set this explicitly, so it is trusted and returned as-is.
    ovr_dir, _ = find_up(cwd, [".claude/tdd-test-cmd"])
    if ovr_dir:
        try:
            for line in (ovr_dir / ".claude" / "tdd-test-cmd").read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
        except Exception:
            pass

    # 2. conventional detection by project marker file
    root, marker = find_up(
        cwd, ["package.json", "pyproject.toml", "pytest.ini", "Cargo.toml", "go.mod"]
    )
    if not root:
        return None

    if marker == "package.json":
        try:
            pkg = json.loads((root / "package.json").read_text())
        except Exception:
            return None
        if "test" not in pkg.get("scripts", {}):
            return None  # no test script -> nothing to enforce
        if (root / "pnpm-lock.yaml").exists():
            pm = "pnpm"
        elif (root / "yarn.lock").exists():
            pm = "yarn"
        elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            pm = "bun"
        else:
            pm = "npm"
        if not shutil.which(pm):
            return None
        return f"{pm} run test" if pm == "bun" else f"{pm} test"

    if marker in ("pyproject.toml", "pytest.ini"):
        return "python3 -m pytest -q" if pytest_available() else None

    if marker == "Cargo.toml":
        return "cargo test" if shutil.which("cargo") else None

    if marker == "go.mod":
        return "go test ./..." if shutil.which("go") else None

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
        marker.unlink()
    except Exception:
        pass

    cmd = detect_test_command(cwd)
    if not cmd:
        sys.exit(0)  # unknown project -> skip silently

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True,
        )
    except Exception as exc:
        # could not even launch the suite -> do not block on infra failure
        print(f"TDD hook: could not run tests ({exc}).", file=sys.stderr)
        sys.exit(0)

    # pytest exit 5 == "no tests collected" -> treat as non-blocking
    if result.returncode == 0 or (result.returncode == 5 and "pytest" in cmd):
        sys.exit(0)

    tail = (result.stdout + result.stderr).strip().splitlines()[-40:]
    msg = (
        f"Tests are NOT green. You changed code this turn but `{cmd}` failed "
        f"(exit {result.returncode}). Reach GREEN before finishing this turn "
        f"— follow the tdd skill: minimal code to pass, then re-run.\n\n"
        f"--- test output (last 40 lines) ---\n" + "\n".join(tail)
    )
    print(msg, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
