#!/usr/bin/env python3
"""TDD hook 3/4 - Stop verifier.

Fires every time Claude finishes responding. When code changed this turn
(per-session marker present), it detects the project's test command, runs
it, and hard-blocks the turn (exit 2) if the suite is not green.

Loop guard: enforces at most once per turn via `stop_hook_active`.
"""
import functools
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

# Oracle marker names, shared by detect_test_command (resolution) and
# _suppressed_ancestor (the skip-path probe) so the two never drift. The
# priority-1 override is checked before conventional markers.
OVERRIDE_NAME = ".claude/tdd-test-cmd"
CONVENTIONAL_MARKERS = ["package.json", "pyproject.toml", "pytest.ini", "Cargo.toml", "go.mod"]


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


def find_up(start, names, stop_at_git=True):
    """Walk up from `start` to find the first dir containing any of `names`,
    bounded to the edited file's own repo.

    At each level the marker is checked FIRST, then the ascent stops at — and
    including — the first directory containing `.git`, so resolution never
    crosses up into an ancestor repo (a monorepo / submodule / $HOME-level
    project; work-interval-tdd#01, ADR-0023). `.git` may be a *file* (git
    worktrees and submodules), so the boundary test is `.exists()`, not
    `.is_dir()`. The marker check runs BEFORE the `.git` stop so a marker
    co-located with `.git` at a repo root still resolves (reordering would break
    the common single-repo case). This `.git` stop also ends cross-repo override
    *inheritance*: a nested repo no longer resolves an override-bearing parent's
    `.claude/tdd-test-cmd` — that is the intended boundary, not just the $HOME
    trade-off below.

    `stop_at_git=False` lifts ONLY the `.git` stop (the $HOME / fs-root caps still
    hold) — the pre-#01 reach. The skip-path probe (`_suppressed_ancestor`) uses
    it to ask what an unbounded ascent *would* have resolved; normal resolution
    keeps the default bound.

    For a path under no git repo the outer fallback is the filesystem root, but
    $HOME is an *exclusive* ceiling: the universal ancestor of every path is
    never treated as a project, so a marker (or repo) rooted exactly at $HOME is
    not resolved — an edit directly at $HOME forgoes even a co-located override.
    `home` is resolved to match `cur`, so the ceiling holds on hosts where $HOME
    traverses a symlink.

    Returns (directory, matched_name) or (None, None).
    """
    home = Path.home().resolve()
    cur = Path(start).resolve()
    while True:
        if cur == home:               # $HOME is an EXCLUSIVE ceiling
            return None, None
        for name in names:            # marker checked BEFORE the .git stop
            if (cur / name).exists():
                return cur, name
        if stop_at_git and (cur / ".git").exists():  # (inclusive) first repo boundary; .git may be a FILE
            return None, None
        if cur == cur.parent:         # outer fallback: filesystem root
            return None, None
        cur = cur.parent


def _git_boundary(start):
    """The repo the edited file lives in: the first ancestor of `start`
    (inclusive) containing `.git`, under the same $HOME-exclusive ceiling as
    find_up — or None when `start` is under no repo (a git-less tree, or $HOME
    itself). This is the boundary the #01 bound stops resolution at."""
    home = Path.home().resolve()
    cur = Path(start).resolve()
    while True:
        if cur == home:
            return None
        if (cur / ".git").exists():           # `.git` dir OR file
            return cur
        if cur == cur.parent:
            return None
        cur = cur.parent


def _suppressed_ancestor(start):
    """Skip-path probe (work-interval-tdd#07): the caller invokes this only when
    the bounded `detect_test_command(start)` resolved no oracle. Returns
    `(repo_root, ancestor_dir)` when LIFTING the #01 `.git` bound WOULD have
    resolved a *runnable* oracle strictly ABOVE the edited file's repo — i.e.
    enforcement was genuinely available and the bound suppressed it — else `None`.

    Faithful by construction, and the reason this satisfies A2: it asks the SAME
    resolver (`detect_test_command`) with the bound lifted (`stop_at_git=False` =
    the pre-#01 reach), so it inherits every runner / test-script / override-parse
    / override-before-conventional priority check. A bare ancestor marker whose
    runner is absent (pytest-less pyproject.toml, a script-less package.json, …)
    therefore resolves to NOTHING here too — exactly the ADR-0024 silent-skip
    classes — and is never reported as suppressed. Only a genuinely runnable
    ancestor oracle is. `root in repo_root.parents` keeps it to *strict* ancestors
    (an in-repo oracle is never a self-suppression), independent of call context.
    """
    repo_root = _git_boundary(start)
    if repo_root is None:
        return None                           # no .git boundary -> nothing bounded
    root, cmd = detect_test_command(start, stop_at_git=False)
    if root and cmd and root in repo_root.parents:
        return repo_root, root                # runnable oracle suppressed above bound
    return None


@functools.lru_cache(maxsize=1)
def _pytest_importable():
    """returncode==0 iff `import pytest` succeeds. Raises on a transient probe
    failure so it is NOT memoized (lru_cache never caches a raise) and the next
    call re-probes: a raising failure (timeout / OSError), AND a subprocess that
    *completes* but was killed by a signal (returncode < 0 — OOM `-9`, segfault
    `-11`), which is abnormal/load-correlated, not a definitive import verdict. A
    non-negative exit is definitive (0 = importable; >0 = genuinely absent) and is
    cached — keeping the per-process memoization that the perf fix needs.
    """
    proc = subprocess.run(
        ["python3", "-c", "import pytest"],
        capture_output=True, timeout=15,
    )
    if proc.returncode < 0:
        raise RuntimeError(f"pytest probe killed by signal {-proc.returncode}")
    return proc.returncode == 0


def pytest_available():
    """True if pytest can be imported by python3 in this environment.

    A DEFINITIVE probe (the subprocess completed) is memoized per process: the
    result cannot change within a single Stop, and the skip-path probe
    (work-interval-tdd#07) can call detect_test_command many times in one turn —
    an uncached subprocess per call is needless churn on the latency-sensitive Stop
    hook (merge-gate claude:finding-0). A TRANSIENT failure is returned uncached so
    one flaky 15s timeout does not fail-open every Python repo for the whole turn
    (merge-gate re-review): the next call re-probes.
    """
    try:
        return _pytest_importable()
    except Exception:
        return False


def detect_test_command(start, stop_at_git=True):
    """Return (project_root, command) for the test suite rooted at or above
    `start`, or (None, None). The command MUST be run with cwd=project_root.

    Priority: an explicit `.claude/tdd-test-cmd` override file, then
    conventional ecosystem detection. Auto-detected commands are returned
    only when their runner is actually installed -> a missing runner is an
    infra gap, not a test failure, and must not block the turn.

    `stop_at_git` is forwarded to both find_up walks: the default bounds
    resolution to the edited file's own repo (#01); the skip-path probe passes
    False to ask what an unbounded (pre-#01) resolution would have produced.
    """
    # 1. per-project override file (first non-empty, non-comment line).
    #    The user set this explicitly, so it is trusted and returned as-is.
    ovr_dir, _ = find_up(start, [OVERRIDE_NAME], stop_at_git=stop_at_git)
    if ovr_dir:
        try:
            for line in (ovr_dir / ".claude" / "tdd-test-cmd").read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return ovr_dir, line
        except Exception:
            pass

    # 2. conventional detection by project marker file
    root, marker = find_up(start, CONVENTIONAL_MARKERS, stop_at_git=stop_at_git)
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
    # Dedup the resolved dirs: several edited files in one dir resolve the same
    # oracle, so detect_test_command / the skip-path probe run once per dir, not
    # once per file — keeps the work off the Stop hot path (merge-gate finding-0).
    start_dirs = list(dict.fromkeys(
        str(Path(p).resolve().parent) for p in edited)) or [cwd]

    # Dedup by resolved project root: many edited files in one repo -> one run;
    # files spanning repos -> one run per repo (each changed repo is verified).
    targets = {}
    suppressed = {}  # repo_root -> ancestor_dir; dedup -> one diagnostic/repo
    for start in start_dirs:
        root, cmd = detect_test_command(start)
        if root and cmd:
            targets[str(root)] = cmd
            continue
        # Skip path: surface a *suppressed* ancestor oracle (work-interval-tdd#07).
        # The #01 bound resolved nothing here; if a *runnable* oracle would have
        # resolved above the repo boundary, the bound suppressed it — make that
        # observable so a worktree/submodule user does not lose TDD coverage
        # unknowingly. Observability only — the ancestor's suite is still NOT run.
        # The probe re-runs the resolver unbounded, so it stays silent for the
        # ADR-0024 no-RUNNABLE-oracle classes (returns None there), not just the
        # no-marker case.
        sup = _suppressed_ancestor(start)
        if sup:
            repo_root, anc_dir = sup
            suppressed[str(repo_root)] = str(anc_dir)

    for repo_root, anc_dir in suppressed.items():
        print(f"TDD hook: enforcement suppressed for {repo_root} — a runnable oracle "
              f"resolves at ancestor {anc_dir}, but resolution is bounded to the "
              f"edited file's own repo (work-interval-tdd#01), so it is not run here. "
              f"Declare a `.claude/tdd-test-cmd` in {repo_root} to enable TDD coverage.",
              file=sys.stderr)

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
