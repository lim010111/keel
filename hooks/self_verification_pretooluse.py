#!/usr/bin/env python3
"""Self-verification gate — PreToolUse evasion recorder (issue self-verify#01 B2).

Records (does NOT block) the two Claude-visible evasions of the commit-msg gate:
  - `git commit --no-verify` / `-n` (the bare escape hatch — C3)
  - scope-narrowed test invocations (e.g. `pytest -k ...`, `pytest path::case`,
    a single test file) that run a passing subset instead of the canonical oracle (#3)

so the bypass is VISIBLE in the out-of-repo audit log rather than silent. Always
exits 0 — recording, never blocking (the actual #3 neutralisation is the
commit-msg hook re-running the FULL oracle; this is the forensic record of the
attempt).

DORMANT (issue E1): lands as a file only; NOT wired into settings.json during the
#31 window. Activation registers it as a PreToolUse(Bash) hook post-#31.

Asymmetry (C3): this is the Claude-only path. A Codex/human `--no-verify` is
unrecorded — a skipped commit-msg hook cannot record its own skip, and this hook
only sees Claude's Bash calls. The agent-agnostic recorded path is B5's trailer.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import self_verification as sv  # noqa: E402

# F4: detection runs on shlex TOKENS within each shell segment, not a regex over
# the raw string. A regex could not tell a real flag from the same characters
# sitting inside a quoted commit message (`git commit -m "add -n flag"`) or a
# chained command (`git commit -m x && grep -rn foo`); both were false positives
# that poison the observe->enforce audit dataset. shlex (posix, punctuation_chars)
# honours quotes and tokenises &&/||/;/| so each command is judged on its own argv.
# top-level command separators / grouping that delimit one invocation from the
# next. Grouping chars `( ) { }` are included (re-review finding) so `(git commit
# -n)` anchors `git` as the program rather than `(`.
_SEP = {"&&", "||", ";", "|", "&", "(", ")", "{", "}"}
# short commit options that CONSUME the rest of an attached cluster as their value
# (so chars after them are the value, not further flags): -m msg, -F file, -C/-c
# commit, -S<keyid> (attached), -t template. -S stays here for the ATTACHED form
# `-Sn` (= sign with keyid `n`); the SPACE-separated `-S -n` is handled below.
_COMMIT_VALUE_SHORT = set("mFCcSt")
# commit options that consume the NEXT space-separated token as their value.
# NB: -S/--gpg-sign are deliberately ABSENT (F4 finding A): git's signing flags
# take an OPTIONAL, attached-only argument (`-Skeyid`/`--gpg-sign=keyid`); a
# following ` -n` is real --no-verify, not the keyid — listing them here made
# `git commit -S -n` swallow the -n and silently drop a genuine bypass.
_COMMIT_VALUE_OPTS = {
    "-m", "--message", "-F", "--file", "-C", "--reuse-message",
    "-c", "--reedit-message", "--author", "--date",
    "-t", "--template", "--fixup", "--squash", "--trailer",
}
# git GLOBAL options (before the subcommand) that consume the next token, so the
# real subcommand is the first positional past them (F4 finding C).
_GIT_GLOBAL_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                          "--exec-path", "--config-env"}
_PYTEST_NARROW_TOKENS = {"-k", "--last-failed", "--lf", "-lf", "-x", "--exitfirst"}
_TEST_FILE_ARG = re.compile(r"(.*/)?test_\w+\.py$")


def _segments(command):
    """shlex-tokenise (honouring quotes; dropping `#` comments) then split into
    command segments at top-level shell operators/grouping. Raises ValueError on a
    parse error (e.g. unbalanced quotes) so the caller can fall back."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    tokens = list(lex)
    segs, cur = [], []
    for t in tokens:
        if t in _SEP:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


# Known observe-phase parser limitations (this is a forensic, record-not-block,
# accident-not-malice recorder — C3; not a complete shell parser). Deliberately
# NOT handled here; each is a dataset-quality gap to revisit before observe->enforce
# activation, tracked in the issue's Follow-up:
#   - heredoc bodies: a `git commit --no-verify` line in a `<<EOF` body is data,
#     not a command, but is recorded as a bypass (false positive).
#   - backslash-newline continuation glued to a flag (`git commit \<nl>-n`): shlex
#     folds a literal newline onto the token, so the bypass is missed (false neg).
#     The natural space-after form (`... \<nl> -n`) IS detected.
#   - nested interpreters (`bash -c "git commit -n"`) / command substitution: the
#     inner command is an opaque quoted token and is not classified (deferred case B).
def _physical_segments(command):
    """Like _segments, but also treats an unquoted NEWLINE as a command separator
    (re-review finding: newline is the dominant separator in agent bash blocks;
    shlex folds it into whitespace, which would merge `git add -A` and a following
    `git commit --no-verify` into one segment — dropping the real bypass AND
    mis-attributing the next line's flags to the commit). Accumulates physical
    lines until they parse, so a newline INSIDE a quoted message is preserved.
    Raises ValueError only if the whole command has a genuinely unbalanced quote."""
    segs, buf = [], ""
    for line in command.split("\n"):
        buf = line if not buf else buf + "\n" + line
        try:
            segs.extend(_segments(buf))
        except ValueError:
            continue  # a quote spans into the next line — keep buffering
        buf = ""
    if buf.strip():
        raise ValueError("unbalanced quotes")
    return segs


def _commit_args(seg):
    """If `seg`'s program is git and its subcommand is `commit`, return the arg
    tokens after `commit`; else None.

    Anchors to the program — the first token past any leading VAR=val assignment
    (F4 finding D: `echo git commit -n` must NOT count, git is not seg[0]) — and
    requires `commit` to be the real subcommand, the first positional after git's
    own global options (F4 finding C: `git log --grep commit -n` must NOT count,
    `commit` there is a --grep term). Tolerates a path-qualified git."""
    i = 0
    while i < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]):
        i += 1  # skip leading env-assignment prefix (FOO=bar git commit ...)
    if i >= len(seg):
        return None
    prog = seg[i]
    if not (prog == "git" or prog.endswith("/git")):
        return None
    j = i + 1
    while j < len(seg):
        t = seg[j]
        if t in _GIT_GLOBAL_VALUE_OPTS:
            j += 2  # global opt + its value
            continue
        if t.startswith("-"):
            j += 1  # other global flag
            continue
        return seg[j + 1:] if t == "commit" else None  # first positional = subcommand
    return None


def _args_have_no_verify(args):
    """True iff the commit argv passes --no-verify (or a short cluster whose `n`
    is reached before any value-bearing flag: `-nm` is no-verify, `-mn` is `-m n`)."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            break  # end-of-options (F4 finding E): everything after is a pathspec
        if a == "--no-verify":
            return True
        if a in _COMMIT_VALUE_OPTS:
            skip = True
            continue
        if a.startswith("--"):
            continue
        if a.startswith("-") and len(a) > 1:
            for ch in a[1:]:
                if ch in _COMMIT_VALUE_SHORT:
                    break  # rest of the cluster is this flag's value
                if ch == "n":
                    return True
    return False


def _is_pytest_invocation(seg):
    """seg's program is pytest. Delegates to sv._toks_invoke_pytest so the
    scope-narrowed-test classifier recognises the same forms as the F1 exit-5
    router: bare `pytest`/`py.test`, `python[3[.x]] -m pytest` (incl. glued
    `-mpytest`), and runner wrappers (`uv run pytest`, `poetry run pytest`, ...).
    The strict basename match (finding G) excludes `pythonista -m pytest`."""
    return sv._toks_invoke_pytest(seg)


def _pytest_is_narrowed(seg):
    """A pytest segment runs a passing subset: -k expr, nodeid (path::case),
    --last-failed/-x, or a single test-file path argument."""
    for t in seg:
        if t in _PYTEST_NARROW_TOKENS:
            return True
        if not t.startswith("-") and ("::" in t or _TEST_FILE_ARG.match(t)):
            return True
    return False


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


def classify(command):
    """Return (event, detail) for a bypass-worthy command, or (None, None).

    Judged per shell segment on tokens (F4): a `--no-verify`/`-n` or a pytest
    narrowing flag is only counted when it is a real argv token of a `git commit`
    / pytest invocation in its own segment — never when it appears inside a quoted
    message or in a chained command."""
    try:
        segs = _physical_segments(command)
    except ValueError:
        # unbalanced quotes etc. — best-effort on the raw string, reusing the
        # pre-F4 word-boundary anchor (F4 finding F) so it still cannot fire on
        # quoted/cluster message text but does not drop the short `-n` form. (A
        # command shlex cannot parse usually fails the shell too, so this path is
        # rarely a real executed bypass.)
        if re.search(r"\bgit\b[^&;|]*\bcommit\b[^&;|]*?"
                     r"(--no-verify|(?<!\S)-[a-zA-Z]*n[a-zA-Z]*\b)", command):
            return "no-verify-commit", command.strip()[:200]
        return None, None
    for seg in segs:
        args = _commit_args(seg)
        if args is not None and _args_have_no_verify(args):
            return "no-verify-commit", command.strip()[:200]
    for seg in segs:
        if _is_pytest_invocation(seg) and _pytest_is_narrowed(seg):
            return "scope-narrowed-test", command.strip()[:200]
    return None, None


def main():
    payload = read_input()
    if str(payload.get("tool_name", "")) != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", ""))
    if not command:
        return 0
    event, detail = classify(command)
    if not event:
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    root = sv.repo_root(cwd)
    if root is None:
        return 0
    # Only record for repos that actually opt into the gate (have the section).
    if not sv.load_config(root)["section_present"]:
        return 0
    sv.record_bypass(root, event=event, source="pretooluse", detail=detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
