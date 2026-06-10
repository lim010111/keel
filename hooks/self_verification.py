#!/usr/bin/env python3
"""Self-verification gate — commit-msg hook (oracle-integrity enforcement).

Grounding: ADR-0001 (oracle integrity, not review), ADR-0013 (out-of-repo
append-only audit log; threat model held at the well-intentioned agent),
ADR-0015 (stage = commit-msg, NOT pre-commit). Spec: claude-harness-work
`.scratch/self-verification-gate/issues/01-oracle-integrity-core.md`.

DORMANT (issue E1): this is a helper/template only. It is NOT wired into any
`.git/hooks` during the #31 measurement window. Activation (wiring into
`<repo>/.git/hooks/commit-msg`, observe-mode on claude-config) is the post-#31
follow-up. Landing this code touches zero merge-gate code.

Why commit-msg, not pre-commit (ADR-0015): the audited bypass lane (B5) is a
`Self-Verify-Bypass:` commit-message trailer, and a pre-commit hook cannot read
the in-flight message (it gets no message argument and `.git/COMMIT_EDITMSG` is
absent/stale at pre-commit time). The commit-msg hook receives the final message
file as argv[1], so the trailer is readable at decision time. A non-zero exit
aborts the commit (the block). Invoked as:  self_verification.py <msg-file>

Threat model (ADR-0013 / C2): durable-against-ACCIDENT (a well-intentioned agent
making honest errors), NOT tamper-proof-against-MALICE. The out-of-repo
append-only log survives a working-tree edit; it is deliberately not defended
against an agent with shell access.

Escape-hatch audit asymmetry (C3): the commit-msg block (A3) and the
trailer-based audited bypass lane (B5) are both git-native / agent-agnostic
(Codex, antigravity, humans — not just Claude). The bare `git commit --no-verify`
*escape hatch* skips this hook entirely, so a skipped commit-msg hook cannot
record its own skip; `--no-verify` is recorded only via the Claude-only
PreToolUse path (self_verification_pretooluse.py). The agent-agnostic *recorded*
path is therefore B5's trailer, not `--no-verify`.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path

# Git's well-known empty-tree object (base for a repo's first commit).
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_BYPASS_TRAILER = "Self-Verify-Bypass"
ORACLE_TIMEOUT_SECONDS = 600

# The [self-verification] oracle-command keys (A1). Single source of truth so the
# config parser and the Task B oracle-policy weakening detector cannot drift.
ORACLE_KINDS = ("test", "typecheck", "lint")


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------
def git(cwd, args, env=None, text=True):
    """Run a git command; return (returncode, stdout). stderr is discarded."""
    full = {**os.environ, **(env or {})}
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=text, env=full,
            timeout=120,
        )
        return r.returncode, (r.stdout if text else r.stdout)
    except Exception:
        return 1, ("" if text else b"")


def repo_root(cwd):
    rc, out = git(cwd, ["rev-parse", "--show-toplevel"])
    if rc != 0 or not out.strip():
        return None
    return Path(out.strip())


def head_sha(root):
    rc, out = git(root, ["rev-parse", "HEAD"])
    return out.strip() if rc == 0 and out.strip() else None


# --------------------------------------------------------------------------
# out-of-repo audit root (ADR-0013 / C1) — repo-hash-keyed, never in the repo
# --------------------------------------------------------------------------
def repo_hash(root):
    """sha1(str(root))[:16] — the recipe copied (NOT imported) from
    merge_gate_scheduler.repo_state_dir; importing it would root under
    ~/.claude/hooks/.merge-gate-state, the wrong tree (C1)."""
    return hashlib.sha1(str(root).encode()).hexdigest()[:16]


def state_dir(root):
    """`~/.claude/.scratch/verification-gate/<repo-hash>/` — a DIRECTORY holding
    bypass.jsonl (C1) + weakening.jsonl (B4). Overridable for tests."""
    base = os.environ.get(
        "SELF_VERIFICATION_STATE_ROOT",
        str(Path.home() / ".claude" / ".scratch" / "verification-gate"),
    )
    return Path(base) / repo_hash(root)


def append_jsonl(path, record):
    """Best-effort-then-allow (ADR-0013 accident-not-malice): create the parent
    on first use; on any write failure swallow-and-continue rather than block the
    commit. Returns True on success, False on swallowed failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def _now():
    # Wall clock for the audit timestamp; isolated so tests can monkeypatch.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_bypass(root, **fields):
    rec = {"ts": _now(), "repo_hash": repo_hash(root), **fields}
    return append_jsonl(state_dir(root) / "bypass.jsonl", rec)


def record_weakening(root, **fields):
    rec = {"ts": _now(), "repo_hash": repo_hash(root), **fields}
    return append_jsonl(state_dir(root) / "weakening.jsonl", rec)


# --------------------------------------------------------------------------
# config (harness.toml [self-verification]) — A1
# --------------------------------------------------------------------------
def _worktree_toml_bytes(root):
    p = Path(root) / "harness.toml"
    try:
        return p.read_bytes() if p.exists() else None
    except Exception:
        return None


def _staged_toml_bytes(root):
    """harness.toml as it exists in the git index = the version in the tree being
    committed (F0). A5 consistency: the attestation's config must come from the
    tree under attestation, not the dirty working copy — otherwise an unstaged
    edit to the oracle command governs a commit that does not contain it. Returns
    bytes, or None if harness.toml is not staged/tracked."""
    try:
        r = subprocess.run(
            ["git", "show", ":harness.toml"],
            cwd=str(root), capture_output=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _head_toml_bytes(root):
    """harness.toml as it exists at HEAD (the parent commit) = the oracle policy
    BEFORE this commit. Tree-anchored (`git show HEAD:harness.toml`), NOT the
    working copy — the Task B baseline must be the committed policy, else an
    unstaged working-tree edit to the oracle would make staged==baseline and
    suppress the weakening row (the same dirty-copy leak Task A closes). Returns
    bytes, or None if harness.toml is absent at HEAD (first commit / newly added
    — no prior policy to weaken)."""
    try:
        r = subprocess.run(
            ["git", "show", "HEAD:harness.toml"],
            cwd=str(root), capture_output=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _config_from_toml_bytes(raw):
    cfg = {
        "oracles": [],
        "exempt_globs": [],
        "bypass_trailer": DEFAULT_BYPASS_TRAILER,
        "section_present": False,
    }
    if raw is None:
        return cfg
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except Exception:
        return cfg
    sec = parsed.get("self-verification")
    if not isinstance(sec, dict):
        return cfg
    cfg["section_present"] = True
    for kind in ORACLE_KINDS:
        cmd = sec.get(kind)
        if isinstance(cmd, str) and cmd.strip():
            cfg["oracles"].append((kind, cmd.strip()))
    eg = sec.get("exempt_globs")
    if isinstance(eg, list):
        cfg["exempt_globs"] = [g for g in eg if isinstance(g, str)]
    bt = sec.get("bypass_trailer")
    if isinstance(bt, str) and bt.strip():
        cfg["bypass_trailer"] = bt.strip()
    return cfg


def load_config(root, staged=False):
    """Read `[self-verification]` from harness.toml. Returns a dict with keys:
    oracles (list[(kind, cmd)]), exempt_globs, bypass_trailer, section_present
    (bool). Missing/empty section => oracles == [].

    staged=True reads the index version (A5: the config in the committed tree
    governs the attestation — F0); default reads the working tree (the PreToolUse
    runtime check, where no commit is in flight, wants the live policy)."""
    raw = _staged_toml_bytes(root) if staged else _worktree_toml_bytes(root)
    return _config_from_toml_bytes(raw)


def resolve_oracles(root, cfg):
    """The canonical oracle command list (from the STAGED tree), or [] with a
    reason-kind.

    F0/A5 invariant — oracle SELECTION, like execution, must come from the tree
    under attestation, never the dirty working copy. So the commit-msg
    attestation uses ONLY the explicitly-declared staged `[self-verification]`
    oracle (load_config(staged=True) = `git show :harness.toml`). It does NOT
    fall back to detect_test_command, which is inherently WORKING-TREE-coupled: it
    reads `.claude/tdd-test-cmd` content (returned verbatim AS the command),
    marker-file existence, and package.json contents — letting an UNSTAGED edit
    select the oracle for a commit it is not part of (the auto-detect analog of
    the F0 leak). Running detect against a materialised /tmp tree would only
    RELOCATE the leak: tdd_verify.find_up stops at $HOME/fs-root, so it ascends
    above the temp root and re-reads out-of-tree markers (the design-panel finding
    that ruled out the staged-auto-detect option — ADR-0016).

    A2's auto-detect is retained where its working-tree semantics are correct —
    tdd_verify's Stop-time turn-time PRIVATE feedback — NOT the commit-time
    attestation, which is a different contract (attest what LANDS). Do not
    re-unify the two paths or the leak reopens.

    Returns (oracles, reason); reason is None when oracles is non-empty, else it
    distinguishes a declared-but-empty section ('config-empty') from an absent one
    ('no-self-verification-section') so the observe->enforce dataset can tell a
    misconfigured repo from an un-onboarded one — both derived purely from the
    STAGED config, with no working-tree read."""
    if cfg["oracles"]:
        return cfg["oracles"], None
    reason = "config-empty" if cfg["section_present"] else "no-self-verification-section"
    return [], reason


# --------------------------------------------------------------------------
# staged-tree materialisation + oracle run (A5 / A3 / B1)
# --------------------------------------------------------------------------
def staged_tree_sha(root):
    """`git write-tree` of the current index = the EXACT tree being committed
    (A5). At commit-msg time the index already holds the staged content."""
    rc, out = git(root, ["write-tree"])
    return out.strip() if rc == 0 and out.strip() else None


def materialize_tree(root, tree_sha, dest):
    """Extract <tree_sha> into <dest> via `git archive` (never touches the real
    working tree — the A5 fix for the stash dance's partial-staging corruption)."""
    try:
        r = subprocess.run(
            ["git", "archive", "--format=tar", tree_sha],
            cwd=str(root), capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            return False
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            # filter='data' (Python 3.12+) is both the safe extractor and the
            # future default; pass it explicitly so a -W error run does not turn
            # the 3.12 DeprecationWarning into a swallowed failure (which would
            # fail-OPEN the gate). Fall back for pre-3.12 where filter= is absent.
            try:
                tar.extractall(dest, filter="data")
            except TypeError:
                tar.extractall(dest)
        return True
    except Exception:
        return False


def _oracle_env():
    """The oracle runs against the materialised tree (cwd=tmp), so it must NOT
    inherit git's hook-injected GIT_* vars (F2): commit-msg is a git hook, and
    git exports GIT_DIR / GIT_INDEX_FILE / GIT_PREFIX / ... pointing at the REAL
    repo. An oracle that shells out to git would otherwise read or mutate the
    parent repo's index instead of erroring cleanly in the (non-git) tmp tree.
    Strip every GIT_* var; our own git calls use git() with the full env and are
    unaffected."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run_oracle_cmd(cmd, cwd, timeout):
    """Run one oracle command. Returns (returncode, output, status) where status
    is 'ran' | 'timeout' | 'launch-error'. Output is decoded with
    errors='replace' so a non-UTF-8 byte in a FAILING oracle's output can never
    flip a red verdict into a swallowed decode error (fail-open). On timeout the
    whole process group is killed (start_new_session) so a hung shell's
    grandchildren do not orphan past the hook."""
    try:
        p = subprocess.Popen(
            cmd, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, env=_oracle_env(),
        )
    except Exception:
        return 1, "", "launch-error"
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            p.communicate(timeout=5)
        except Exception:
            pass
        return 1, "", "timeout"
    text = out.decode("utf-8", errors="replace") if out else ""
    return p.returncode, text, "ran"


# Runner wrappers that delegate straight to the program after `run` (uv run
# pytest, poetry run pytest, ...). NB: tox is deliberately excluded — `tox -e py`
# builds an env and hides the runner, so its exit 5 stays a plain failure.
_PYTEST_RUNNER_WRAPPERS = {"uv", "poetry", "pdm", "hatch", "rye"}


def _toks_invoke_pytest(toks):
    """True iff the token list's PROGRAM is pytest: bare `pytest`/`py.test`
    (optionally path-qualified), a python launcher running `-m pytest` (incl. the
    glued `-mpytest`), or a runner wrapper delegating to pytest (`uv run pytest`,
    `poetry run pytest`, ...). Shared by the F1 exit-5 router and the F4
    scope-narrow classifier so the two recognise the same forms (finding H + the
    re-review's wrapper/glued regression)."""
    # peel runner-wrapper prefixes ITERATIVELY (never recursively — a pathological
    # `uv run uv run ...` nest would otherwise blow the recursion limit).
    while (len(toks) >= 3
           and toks[0].rsplit("/", 1)[-1] in _PYTEST_RUNNER_WRAPPERS
           and toks[1] == "run"):
        toks = toks[2:]
    if not toks:
        return False
    base = toks[0].rsplit("/", 1)[-1]
    if base in ("pytest", "py.test"):
        return True
    if re.fullmatch(r"python(\d+(\.\d+)?)?", base):
        rest = toks[1:]
        if "-m" in rest and "pytest" in rest:
            return True
        return "-mpytest" in rest  # glued -m form
    return False


def _cmd_invokes_pytest(cmd):
    """True iff the oracle command's program is pytest (see _toks_invoke_pytest) —
    not merely a string containing 'pytest' (F1 finding H: the coarse
    `"pytest" in cmd` substring both false-positived, e.g. `make pytest-ci`, and
    false-negatived wrappers). A parse error or a genuinely indirect wrapper
    (e.g. `tox -e py`) falls through to False, so its exit 5 is a plain failure
    rather than a spurious empty-oracle row."""
    try:
        return _toks_invoke_pytest(shlex.split(cmd, comments=True))
    except ValueError:
        return False


def run_oracles(root, tree_sha, oracles):
    """Run every canonical oracle command against the materialised staged tree
    (A3/B1: the hook runs the FULL declared oracle out-of-band, regardless of any
    scope-narrowed command the agent ran). Returns (verdict, details) where
    verdict is one of 'pass' | 'fail' | 'empty' | 'infra'.

    Fail-CLOSED bias for the ambiguous cases (an attestation must not be tricked
    into green): a TIMEOUT counts as a failure (a hung suite cannot attest green),
    not 'infra'. Only a genuine 'could not launch / could not materialise' is
    'infra' (non-block) — that is an environment gap, not a red oracle.

    'empty' (F1): pytest exit 5 == "no tests collected". That is NOT a green
    attestation — it is the execution-time mirror of A6's no-oracle case (zero
    test signal), e.g. after a #5 delete-all-tests. It must be RECORDED, never
    silently passed; main() logs it to the audit lane. Recorded-not-blocked
    (an enforce-time decision is deferred to post-#31 calibration)."""
    with tempfile.TemporaryDirectory(prefix="self-verify-") as tmp:
        if not materialize_tree(root, tree_sha, tmp):
            return "infra", [{"kind": "materialize", "detail": "git archive failed"}]
        failures = []
        empties = []
        for kind, cmd in oracles:
            rc, output, status = _run_oracle_cmd(cmd, tmp, ORACLE_TIMEOUT_SECONDS)
            if status == "launch-error":
                return "infra", [{"kind": kind, "detail": "could not launch"}]
            if status == "timeout":
                failures.append({"kind": kind, "cmd": cmd, "exit": "timeout",
                                 "tail": f"oracle exceeded {ORACLE_TIMEOUT_SECONDS}s "
                                         "— treated as RED"})
                continue
            if rc == 5 and _cmd_invokes_pytest(cmd):
                empties.append({"kind": kind, "cmd": cmd, "exit": rc,
                                "detail": "pytest collected no tests (exit 5)"})
                continue
            if rc == 0:
                continue
            tail = output.strip().splitlines()[-20:]
            failures.append({"kind": kind, "cmd": cmd, "exit": rc,
                             "tail": "\n".join(tail)})
        if failures:
            return "fail", failures
        if empties:
            return "empty", empties
        return "pass", []


# --------------------------------------------------------------------------
# bypass trailer (B5) — read from the commit-msg message file
# --------------------------------------------------------------------------
def parse_bypass_trailer(msg_file, trailer_key):
    """Return the non-empty reason from a `<trailer_key>: <reason>` trailer in
    the commit-message file, or None. Uses `git interpret-trailers --parse`,
    falling back to a manual body scan (mirrors merge_gate_local.tip_bypass_reason
    — but reads the message FILE, since at commit-msg time no commit exists yet)."""
    try:
        text = Path(msg_file).read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        r = subprocess.run(
            ["git", "interpret-trailers", "--parse"],
            input=text, capture_output=True, text=True, timeout=30,
        )
        parsed = r.stdout if r.returncode == 0 else text
    except Exception:
        parsed = text
    prefix = f"{trailer_key}:"
    for line in parsed.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            reason = line[len(prefix):].strip()
            if reason:
                return reason
    return None


# --------------------------------------------------------------------------
# oracle-weakening detection (#4-6) — Python-only, observe mode (B3/B4)
# --------------------------------------------------------------------------
TEST_FILE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py)$")
TEST_DIR = re.compile(r"(^|/)tests?/")
CONFIG_FILE = re.compile(
    r"(^|/)(pyproject\.toml|setup\.cfg|tox\.ini|\.coveragerc|\.flake8|"
    r"ruff\.toml|\.ruff\.toml)$"
)

SKIP_ADDED = re.compile(
    r"@(pytest\.mark\.(skip|xfail)|unittest\.skip)|(^|\W)pytest\.skip\("
)
ASSERT_TRUE = re.compile(r"^\s*assert\s+(True|1)\s*(#.*)?$")
DEF_TEST = re.compile(r"^\s*def\s+test_\w+")
# #6: a config change that makes the oracle pass more easily.
FAIL_UNDER = re.compile(r"fail[_-]?under")
LINT_DISABLE = re.compile(r"(noqa|# *type: *ignore|ignore\s*=|per-file-ignores|disable=)")


def _is_test_path(path):
    return bool(TEST_FILE.search(path) or TEST_DIR.search(path))


def _glob_to_regex(g):
    """Translate a path glob into a regex, with real recursive `**` semantics
    (fnmatch treats `**` as a plain `*`, so `**/*.md` would miss a top-level
    `notes.md`)."""
    i, n, out = 0, len(g), []
    while i < n:
        if g[i:i + 3] == "**/":
            out.append("(?:.*/)?"); i += 3
        elif g[i:i + 2] == "**":
            out.append(".*"); i += 2
        elif g[i] == "*":
            out.append("[^/]*"); i += 1
        elif g[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(g[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def _exempt(path, exempt_globs):
    for g in exempt_globs:
        if _glob_to_regex(g).match(path):
            return True
    return False


def detect_weakening(root, exempt_globs):
    """Coarse, observe-only structural scan of the staged diff for #4-6. False
    negatives are explicitly tolerated (observe->enforce calibrates post-#31);
    the point is a structured, queryable sink from day one (B4). Returns a list
    of {kind: '#4'|'#5'|'#6', locator: 'file:line' | 'file:key', detail}."""
    rc, names = git(root, ["diff", "--cached", "--name-only", "-z"])
    if rc != 0:
        return []
    files = [f for f in names.split("\0") if f]
    obs = []
    for f in files:
        if _exempt(f, exempt_globs):
            continue
        is_test = _is_test_path(f)
        is_config = bool(CONFIG_FILE.search(f))
        if not (is_test or is_config):
            continue
        rc, diff = git(root, ["diff", "--cached", "-U0", "--", f])
        if rc != 0:
            continue
        new_line = 0
        for line in diff.splitlines():
            if line.startswith("@@"):
                m = re.search(r"\+(\d+)", line)
                new_line = int(m.group(1)) if m else new_line
                continue
            added = line.startswith("+") and not line.startswith("+++")
            removed = line.startswith("-") and not line.startswith("---")
            body = line[1:] if (added or removed) else line
            if is_test:
                if added and SKIP_ADDED.search(body):
                    obs.append({"kind": "#5", "locator": f"{f}:{new_line}",
                                "detail": "added skip/xfail"})
                if added and ASSERT_TRUE.match(body):
                    obs.append({"kind": "#4", "locator": f"{f}:{new_line}",
                                "detail": "assert True (tautology)"})
                if removed and DEF_TEST.match(body):
                    obs.append({"kind": "#5", "locator": f"{f}",
                                "detail": "removed test function"})
                if removed and re.match(r"^\s*assert\b", body):
                    obs.append({"kind": "#4", "locator": f"{f}",
                                "detail": "removed assertion"})
            if is_config:
                # B4 names a config-KEY locator for #6 (not file:line). The raw
                # changed line is kept in detail so the post-#31 observe->enforce
                # promotion has the value-delta (raised vs lowered threshold) it
                # will need — detection itself is direction-blind by design
                # (coarse observe-mode; FPs tolerated).
                if added and FAIL_UNDER.search(body):
                    obs.append({"kind": "#6", "locator": f"{f}:fail_under",
                                "detail": f"coverage threshold change: {body.strip()[:120]}"})
                if added and LINT_DISABLE.search(body):
                    obs.append({"kind": "#6", "locator": f"{f}:lint-ignore",
                                "detail": f"lint rule change: {body.strip()[:120]}"})
            if added:
                new_line += 1
    return obs


def _self_verification_oracles(raw):
    """({kind: command}, parsed_ok) for the [self-verification] oracle keys in a
    harness.toml byte blob. parsed_ok is False ONLY when the blob is present but
    malformed TOML; raw=None (file genuinely absent from the tree) returns
    ({}, True) — an absent file is a real empty oracle set (e.g. the policy file
    was deleted), not a parse failure. Mirrors _config_from_toml_bytes's per-kind
    str/strip validation so the Task B baseline and staged sets are normalised
    identically (a cosmetic whitespace-only change does not register as a
    weakening)."""
    if raw is None:
        return {}, True
    try:
        sec = tomllib.loads(raw.decode("utf-8")).get("self-verification")
    except Exception:
        return {}, False
    out = {}
    if isinstance(sec, dict):
        for k in ORACLE_KINDS:
            v = sec.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    return out, True


def detect_oracle_policy_weakening(root):
    """Observe-only (record, never block) detection of a weakening of the oracle
    POLICY ITSELF: a `[self-verification]` test/typecheck/lint command in
    harness.toml that is REMOVED or CHANGED between HEAD and the staged tree
    (Task B). The #4-6 line-scan targets coverage/lint config + test files; an
    oracle-command edit (`test = "pytest"` -> `test = "true"`, or deleting the
    line/section) slips through it entirely, so a neutered oracle would
    self-attest green with ZERO trace. This closes that observability gap (B4
    ledger), record-not-block per ADR-0013 (a deliberate `test = "true"` is
    malice, scoped out; the layered merge-gate diff review + observe->enforce is
    the defense, not a commit-time block).

    Both sides are git-tree-anchored (staged = `git show :harness.toml`, baseline
    = `git show HEAD:harness.toml`) — never the working copy — so the detector is
    not itself defeated by the dirty-copy leak Task A closes. exempt_globs do NOT
    apply: they exempt source/test/config FILE edits from the #4-6 scan, but the
    oracle declaration is the gate's own policy and must not be exemptable.

    Direction-blind by design (consistent with the #6 coverage/lint detector): an
    oracle CHANGE is recorded with old->new in `detail` for post-#31 calibration,
    not pre-judged weaker-vs-stronger. A newly ADDED oracle (absent at HEAD) is a
    strengthening and is NOT recorded — so a born-weak first declaration
    (`test = "true"` as the very first oracle) is out of scope here (accident-not-
    malice; partly covered at execution time by the F1 empty-oracle record). This
    closes the staged-command-text-change hole, not every neutering (e.g. a
    PATH-shadowed runner leaves the command text unchanged). Returns the same
    {kind:'#6', locator, detail} row shape as detect_weakening."""
    staged, staged_ok = _self_verification_oracles(_staged_toml_bytes(root))
    head, head_ok = _self_verification_oracles(_head_toml_bytes(root))
    if not (staged_ok and head_ok):
        # Only diff when BOTH sides parse. A malformed STAGED config would flood
        # the ledger with spurious 'removed' rows from a syntax typo; a malformed
        # HEAD baseline has no parseable prior policy to weaken (every staged
        # oracle would read as newly-added). Both are handled in one symmetric
        # guard, not left to emerge by accident. Either way the unparseable config
        # is already surfaced by the Task A no-oracle/config-empty record, so we do
        # NOT synthesise a low-signal 'baseline-unparseable' row (the flood-guard
        # rationale). FN tolerated on the observe lane (calibrated post-#31).
        return []
    obs = []
    for k in ORACLE_KINDS:
        old, new = head.get(k), staged.get(k)
        if old is None:
            continue  # newly added oracle = strengthening, not a weakening
        loc = f"harness.toml:self-verification.{k}"
        if new is None:
            obs.append({"kind": "#6", "locator": loc,
                        "detail": f"oracle removed: {k} was {old!r}"})
        elif new != old:
            obs.append({"kind": "#6", "locator": loc,
                        "detail": f"oracle changed: {k} {old!r} -> {new!r}"})
    return obs


# --------------------------------------------------------------------------
# commit-msg entrypoint
# --------------------------------------------------------------------------
BLOCK_MSG = """\
Self-verification gate: the canonical oracle is RED for the tree you are
committing — {summary}

The verdict is this hook's exit code, not any agent claim (ADR-0001). Options:
  1. Reach GREEN, then commit again (the intended path).
  2. Deliberate red commit (e.g. commit-the-red-test-first TDD)? add a trailer
       {trailer}: <reason>
     to this commit message — recorded to the audited bypass lane and allowed.
  3. Escape hatch (unrecorded for non-Claude actors): git commit --no-verify
"""


def main(argv):
    msg_file = argv[1] if len(argv) > 1 else None
    cwd = os.getcwd()
    root = repo_root(cwd)
    if root is None:
        return 0  # not a git repo — nothing to gate

    cfg = load_config(root, staged=True)  # F0: config from the tree being committed
    base = head_sha(root) or EMPTY_TREE
    tree = staged_tree_sha(root)
    bypass = parse_bypass_trailer(msg_file, cfg["bypass_trailer"]) if msg_file else None

    # #4-6 weakening: observe-only (record, never block) — always runs (B3/B4).
    # detect_weakening derives its diff from `git diff --cached` (index vs HEAD =
    # the commit content at commit-msg time); tree/base are recorded on the row,
    # not passed to the detector.
    if tree is not None:
        weakenings = detect_weakening(root, cfg["exempt_globs"])
        weakenings += detect_oracle_policy_weakening(root)  # Task B: oracle-policy file
        for o in weakenings:
            record_weakening(root, tree=tree, parent=base, kind=o["kind"],
                             locator=o["locator"], detail=o["detail"])

    oracles, no_oracle_reason = resolve_oracles(root, cfg)

    # A6: no oracle is RECORD, not silent-pass (a commit-time attestation must not
    # fail open the way tdd_verify's None->pass does for turn-time feedback).
    if not oracles:
        record_bypass(root, event="no-oracle", reason=no_oracle_reason,
                      tree=tree, parent=base)
        return 0

    if tree is None:  # could not even read the staged tree — infra, do not block
        record_bypass(root, event="infra", reason="write-tree-failed", parent=base)
        return 0

    verdict, failures = run_oracles(root, tree, oracles)

    if verdict == "infra":
        record_bypass(root, event="infra", reason="oracle-unrunnable",
                      tree=tree, parent=base, failures=failures)
        return 0  # infra gap is not a red oracle (tdd_verify posture)

    if verdict == "empty":
        # F1: zero test signal (pytest exit 5) — recorded, not silently green and
        # not blocked (record-not-block, mirroring A6; enforce is a post-#31 call).
        record_bypass(root, event="empty-oracle", reason="pytest-collected-no-tests",
                      tree=tree, parent=base, failures=failures)
        return 0

    if verdict == "pass":
        return 0

    # verdict == "fail" — the #1-3 block (A3/B1).
    if bypass:
        record_bypass(root, event="bypass-trailer", reason=bypass,
                      tree=tree, parent=base, failures=failures)
        return 0  # audited bypass lane (B5): recorded + allowed

    summary = "; ".join(
        f"{x.get('kind')} exit {x.get('exit')}" for x in failures
    ) or "oracle failed"
    sys.stderr.write(BLOCK_MSG.format(summary=summary, trailer=cfg["bypass_trailer"]))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
