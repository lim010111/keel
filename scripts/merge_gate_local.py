#!/usr/bin/env python3
"""merge-gate-local — local-profile merge gate (claude-harness-work #30).

A standalone wrapper, run OUTSIDE any Claude Code session (the per-repo
`pre-push` hook calls it), exposing three asymmetric-privilege subcommands
(#30 D1):

  produce   (expensive) — run each reviewer in the configured set (default
            ``[codex]``) in its own fresh context, validate its findings via a
            headless `claude -p "/run-codex-validators …"`, merge per-reviewer
            results (union block — ADR-0011), and write a tuple-keyed artefact.
            May run Codex + headless Claude. The source of truth for freshness
            and artefact format.
  verify    (fast, deterministic) — read the existing artefact's summary.json
            only; check freshness + pass state; exit 0 (pass/advisory) or 1
            (block under client-side-blocking). Runs NO Codex, NO Claude, and
            writes NO artefact. The pre-push hook calls only this.
  force     (manual) — cache-ignoring re-produce.

Design lives in `.scratch/merge-gate/issues/30-local-merge-gate-profile.md`
(claude-harness-work) and ADR-0009/0010/0011/0012. Highlights enforced here:

  * Single shared canonical-diff helper used by produce, the Stop scheduler,
    AND verify (D4) — they must agree on the hashed publication diff. The diff
    is fed to the reviewer INLINE (ADR-0012); the model never self-collects it.
  * Codex reviewer = `codex exec --json --output-schema <schema> --sandbox
    read-only "<adversarial prompt>"` — NOT `codex review` (ADR-0012). Its
    JSONL is normalized to `.result.findings[]` by the "Normalize Codex
    JSONL" step (G1; ported from the since-removed GHA workflow — ADR-0021),
    the shape `/run-codex-validators --codex-json` expects.
  * The validator (uphold/dismiss) runs in its OWN headless context, never the
    implementing session (#24/#26/#29 fail-open lessons); produce sets
    MERGE_GATE_PRODUCER_RUNNING=1 around the headless calls.

This module is import-safe (the hooks and tests import its helpers); the CLI
lives under ``main``.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

# --------------------------------------------------------------------------
# Versions — these feed review_scope_hash (D8) and summary.json schema gating.
# Bump when the corresponding contract changes; never derive them from a
# volatile binary version (a tool bump must not churn the cache — D4).
# --------------------------------------------------------------------------
SCHEMA_VERSION = 1                 # summary.json layout; verify gates on this
CANONICAL_DIFF_ALGO_VERSION = "1"  # the diff algorithm below
VALIDATOR_CONTRACT_VERSION = "1"   # codex-review-validator <output_contract>
ADVERSARIAL_PROMPT_VERSION = "1"   # the vendored adversarial-review prompt

HOME = Path.home()

# --------------------------------------------------------------------------
# Producer runtime root — #36 import-closure + #37 producer-asset hermeticity.
#
# The producer's runtime is FOUR co-located files: the import closure (this
# module in scripts/, merge_gate_scheduler in hooks/) PLUS its vendored assets
# (the adversarial-review prompt + the review-output schema). In a checkout
# OUTSIDE ~/.claude (CI, `git worktree`) the producer must run its OWN code AND
# read its OWN assets, or the two decouple: #36 pinned imports to the checkout,
# but assets still resolving from $HOME meant a checkout with the .py closure but
# missing/different assets would PIN imports yet FALL BACK assets (empty $HOME →
# the SILENT produce-trigger stop reading a non-existent prompt, ADR-0014;
# populated $HOME → review-asset version skew). f60c84a claude:finding-0.
#
# UNIFIED gate (the fix): pin to the checkout ONLY when the COMPLETE runtime set
# is co-located; otherwise EVERY layer falls back to $HOME/.claude together —
# never a partial pin. resolve_import_roots in merge_gate_post_commit.py gates on
# this SAME set (it must decide which merge_gate_local to import BEFORE it can
# import this module, so it evaluates the predicate independently from a
# duplicated relpath list; test_unified_gate_predicates_agree pins the two
# equal). A real $HOME/.claude install has all four under $HOME → byte-identical
# to the pre-#37 hardcoded paths.
# --------------------------------------------------------------------------
# The producer's complete runtime set, as path components relative to CLAUDE_DIR.
# The canonical contract resolve_import_roots (in the post-commit hook) mirrors.
RUNTIME_SET_RELPATHS = (
    ("scripts", "merge_gate_local.py"),
    ("hooks", "merge_gate_scheduler.py"),
    ("scripts", "merge-gate-assets", "adversarial-review.md"),
    ("skills", "setup-merge-gate", "templates", "review-output.schema.json"),
)


def runtime_set_complete(claude_dir: Path) -> bool:
    """True iff the COMPLETE producer runtime set (import closure + assets) is
    co-located under `claude_dir`. The single predicate shared by the asset gate
    (resolve_claude_dir) and the import gate (resolve_import_roots in the
    post-commit hook) so imports and assets never decouple (#37 unified gate)."""
    return all(claude_dir.joinpath(*rel).is_file() for rel in RUNTIME_SET_RELPATHS)


def resolve_claude_dir(module_file: Path, home: Path) -> Path:
    """The CLAUDE_DIR this producer roots its assets at: its OWN checkout
    (module_file is <root>/scripts/merge_gate_local.py, so the root is its
    grandparent) iff the complete runtime set is co-located there, else
    $HOME/.claude. `.resolve()` follows a symlinked install to the real repo, so
    a symlinked module reads the assets next to its REAL location — matching the
    hook's `Path(__file__).resolve()` (f60c84a claude:finding-1)."""
    checkout = module_file.resolve().parent.parent      # <root>/scripts/<this> → <root>
    if runtime_set_complete(checkout):
        return checkout
    return home / ".claude"


CLAUDE_DIR = resolve_claude_dir(Path(__file__), HOME)
ASSETS_DIR = CLAUDE_DIR / "scripts" / "merge-gate-assets"
ADVERSARIAL_PROMPT_PATH = ASSETS_DIR / "adversarial-review.md"
# The schema is reused (not re-vendored) from the byte-identical template copy
# — see merge-gate-assets/PROVENANCE.md.
SCHEMA_PATH = CLAUDE_DIR / "skills" / "setup-merge-gate" / "templates" / "review-output.schema.json"
# The validator agent definition. UNLIKE the prompt + schema (producer-READ
# assets the producer loads from CLAUDE_DIR), the agent is resolved by the
# dispatched headless `claude -p /run-codex-validators` from the ~/.claude
# SETTINGS sources — NOT the producer's checkout (the dispatcher cannot isolate
# settings; see default_validator_runner). So the hash must track the file that
# ACTUALLY executes: root it at $HOME, never CLAUDE_DIR, else a checkout-vs-home
# skew would hash one file while a different agent judges. Its frontmatter
# `model:` is the EFFECTIVE validator model when the
# `[merge-gate.local.validator].model` pin is unset, so its content enters the
# scope hash via validator_agent_sha. It is deliberately NOT in
# RUNTIME_SET_RELPATHS — the producer never reads it, so it must not gate the
# checkout-vs-home pin decision.
VALIDATOR_AGENT_PATH = HOME / ".claude" / "agents" / "codex-review-validator.md"

# Ported from codex-plugin-cc git.mjs formatUntrackedFile: untracked files
# larger than this are excluded from the review tree and recorded, rather than
# bloating the reviewed diff. Tracked changes are never capped.
MAX_UNTRACKED_BYTES = 24 * 1024

# Stable diff flags — ported from git.mjs. No environment-dependent behaviour
# (--no-ext-diff disables any user diff driver; --binary makes binary changes
# part of the canonical bytes; --submodule=diff makes submodule bumps visible).
DIFF_FLAGS = ["--binary", "--no-ext-diff", "--submodule=diff"]

DEFAULT_ARTIFACT_ROOT = ".merge-gate/local"
SEVERITIES = ("critical", "high", "medium", "low")


# --------------------------------------------------------------------------
# Config (harness.toml) — D8.
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "profile": "local",
    "enforcement_policy": "advisory",       # advisory | client-side-blocking
    "freshness_policy": "content",          # content | tool-strict
    "base_ref": "auto",
    "artifact_root": DEFAULT_ARTIFACT_ROOT,
    "review_globs": ["**/*"],
    "ignore_globs": [".merge-gate/**"],
    "blocking_severities": ["critical", "high"],
    "bypass_trailer": "Merge-Gate-Bypass",
    "scheduler": {
        "auto_produce": "stop-debounced",   # off | stop-debounced
        "debounce_seconds": 90,
        "min_interval_seconds": 600,
    },
    "reviewers": ["codex"],
    "reviewer_config": {
        "codex": {"bin": "codex"},
    },
    # [merge-gate.local.validator] (#47): `model` = the validator AGENT's tier
    # alias (the judgment subagent — produce passes it through as the slash
    # `--agent-model`); `dispatcher_model` = the headless `claude -p` session
    # that orchestrates the skill. Unset keys mean "the tool's own default"
    # (agent frontmatter / CLI default).
    "validator": {},
}


class Config:
    """Resolved `[merge-gate]` config for a repo. Missing keys fall back to
    DEFAULT_CONFIG so a partially-written harness.toml still works."""

    def __init__(self, data: dict):
        self._d = data

    @property
    def profile(self) -> str:
        return self._d["profile"]

    @property
    def enforcement_policy(self) -> str:
        return self._d["enforcement_policy"]

    @property
    def freshness_policy(self) -> str:
        return self._d["freshness_policy"]

    @property
    def base_ref(self) -> str:
        return self._d["base_ref"]

    @property
    def artifact_root(self) -> str:
        return self._d["artifact_root"]

    @property
    def review_globs(self) -> list[str]:
        return list(self._d["review_globs"])

    @property
    def ignore_globs(self) -> list[str]:
        return list(self._d["ignore_globs"])

    @property
    def blocking_severities(self) -> list[str]:
        return list(self._d["blocking_severities"])

    @property
    def bypass_trailer(self) -> str:
        return self._d["bypass_trailer"]

    @property
    def reviewers(self) -> list[str]:
        return list(self._d["reviewers"])

    @property
    def scheduler(self) -> dict:
        return dict(self._d["scheduler"])

    def reviewer_args(self, name: str) -> list[str] | None:
        """Per-reviewer expert `args` override, or None when unset. When set it
        enters review_scope_hash (D8)."""
        rc = self._d.get("reviewer_config", {}).get(name, {})
        args = rc.get("args")
        return list(args) if isinstance(args, list) else None

    def reviewer_bin(self, name: str) -> str:
        rc = self._d.get("reviewer_config", {}).get(name, {})
        return rc.get("bin", name)

    def reviewer_cmd(self, name: str) -> list[str] | None:
        """Per-reviewer custom `cmd` override, or None when unset. When set it
        enters review_scope_hash (D8), mirroring reviewer_args. A string cmd is
        normalized to a single-element list so a string-form cmd swap also busts
        the scope hash (it is still a different reviewer program — C5)."""
        rc = self._d.get("reviewer_config", {}).get(name, {})
        cmd = rc.get("cmd")
        if isinstance(cmd, list):
            return list(cmd)
        if isinstance(cmd, str) and cmd:
            return [cmd]
        return None

    def reviewer_model(self, name: str) -> str | None:
        """First-class per-reviewer `model` key (#47), or None when unset.
        Translated to the reviewer CLI's `--model` by the runner; coexisting
        with an args-supplied `--model` is refused fail-closed (set exactly
        one). Enters review_scope_hash."""
        rc = self._d.get("reviewer_config", {}).get(name, {})
        m = rc.get("model")
        return m if isinstance(m, str) and m else None

    def reviewer_reasoning_effort(self, name: str) -> str | None:
        """First-class per-reviewer `reasoning_effort` key (#48), or None when
        unset. codex → `-c model_reasoning_effort=<v>` (official values:
        minimal/low/medium/high/xhigh); claude → `--effort <v>` (official
        values: low/medium/high/xhigh/max, model-dependent). Validated loudly
        at cmd_produce (enum-shaped knob, #47 posture). Enters
        review_scope_hash."""
        rc = self._d.get("reviewer_config", {}).get(name, {})
        v = rc.get("reasoning_effort")
        return v if isinstance(v, str) and v else None

    @property
    def validator_model(self) -> str | None:
        """The validator AGENT's tier alias (#47) — the judgment subagent's
        model, passed through as the slash `--agent-model`. None = the agent
        frontmatter default. Enters review_scope_hash."""
        m = self._d.get("validator", {}).get("model")
        return m if isinstance(m, str) and m else None

    @property
    def validator_dispatcher_model(self) -> str | None:
        """The validator DISPATCHER's model (#47) — the headless `claude -p`
        session that orchestrates the skill; pipeline, not judge. None = the
        CLI default. Enters review_scope_hash."""
        m = self._d.get("validator", {}).get("dispatcher_model")
        return m if isinstance(m, str) and m else None

    @property
    def validator_dispatcher_effort(self) -> str | None:
        """The validator DISPATCHER's `--effort` (#48; official values
        low/medium/high/xhigh/max). The validator AGENT has no per-dispatch
        effort surface (Agent tool exposes only `model`; effort is the agent
        frontmatter's, global by design) — so the agent deliberately has no
        effort key here. None = the CLI default. Enters review_scope_hash."""
        v = self._d.get("validator", {}).get("dispatcher_effort")
        return v if isinstance(v, str) and v else None


def _merge_defaults(base: dict, override: dict) -> dict:
    """Shallow+one-level-nested merge of override onto base (copy of base)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def is_local_profile(repo_root: Path) -> bool:
    """True iff <repo>/harness.toml EXPLICITLY selects the local profile. The
    global hooks self-gate on this — a repo with no harness.toml (or no
    `[merge-gate]` section) must NOT be treated as local, or every repo on the
    machine would get merge-gate marking/scheduling (AC § Scheduler & hooks)."""
    toml_path = repo_root / "harness.toml"
    if not toml_path.exists():
        return False
    try:
        with open(toml_path, "rb") as fh:
            parsed = tomllib.load(fh)
    except Exception:
        return False
    return parsed.get("merge-gate", {}).get("profile") == "local"


def load_config(repo_root: Path) -> Config:
    """Read `[merge-gate]` + `[merge-gate.local*]` from <repo>/harness.toml.

    Backward/forward tolerant: unknown keys ignored, missing keys defaulted.
    The wrapper only ever reads the LOCAL profile's keys (the only profile —
    ADR-0021); leftover foreign keys are ignored, never load-bearing."""
    data = _merge_defaults(DEFAULT_CONFIG, {})
    toml_path = repo_root / "harness.toml"
    if not toml_path.exists():
        return Config(data)
    try:
        with open(toml_path, "rb") as fh:
            parsed = tomllib.load(fh)
    except Exception as e:
        err(f"could not parse {toml_path}: {e}; using defaults")
        return Config(data)
    mg = parsed.get("merge-gate", {})
    if "profile" in mg:
        data["profile"] = mg["profile"]
    local = mg.get("local", {})
    for key in ("enforcement_policy", "freshness_policy", "base_ref",
                "artifact_root", "review_globs", "ignore_globs",
                "blocking_severities", "bypass_trailer"):
        if key in local:
            data[key] = local[key]
    sched = local.get("scheduler", {})
    data["scheduler"] = {**data["scheduler"], **sched}
    producer = local.get("producer", {})
    if "reviewers" in producer:
        data["reviewers"] = producer["reviewers"]
    # Per-reviewer sub-tables: [merge-gate.local.producer.<name>]
    rc = {}
    for name, val in producer.items():
        if isinstance(val, dict):
            rc[name] = val
    if rc:
        data["reviewer_config"] = {**data.get("reviewer_config", {}), **rc}
    # [merge-gate.local.validator] (#47): validator agent/dispatcher models.
    validator = local.get("validator", {})
    if isinstance(validator, dict) and validator:
        data["validator"] = {**data.get("validator", {}), **validator}
    return Config(data)


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def err(msg: str) -> None:
    sys.stderr.write(f"merge-gate-local: {msg}\n")


def glob_to_regex(pattern: str) -> re.Pattern:
    """fnmatch-with-** → regex. Ported verbatim from the docs-only `to_regex`
    of the since-removed GHA workflow template (ADR-0021), preserving its
    glob semantics."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def in_scope(path: str, review_globs: list[str], ignore_globs: list[str]) -> bool:
    """A path is in scope iff it matches some review glob and no ignore glob."""
    inc = [glob_to_regex(g) for g in review_globs if g]
    exc = [glob_to_regex(g) for g in ignore_globs if g]
    if not any(p.match(path) for p in inc):
        return False
    if any(p.match(path) for p in exc):
        return False
    return True


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------
def git(cwd, args, env=None):
    """Run git, returning (returncode, stdout_text). Never raises."""
    full_env = {**os.environ, **(env or {})}
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), env=full_env,
                           capture_output=True)
    except Exception as e:
        return 1, f"{e}"
    return p.returncode, p.stdout.decode("utf-8", "replace")


def git_bytes(cwd, args, env=None) -> tuple[int, bytes]:
    """Run git, returning (returncode, stdout_bytes). For diff output, which
    may contain binary patches — the canonical hash is over these raw bytes."""
    full_env = {**os.environ, **(env or {})}
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), env=full_env,
                           capture_output=True)
    except Exception:
        return 1, b""
    return p.returncode, p.stdout


def git_checked(cwd, args) -> str:
    rc, out = git(cwd, args)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (exit {rc}): {out.strip()}")
    return out.strip()


def repo_root(cwd) -> Path | None:
    rc, out = git(cwd, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        return None
    return Path(out.strip())


def detect_default_branch(cwd) -> str | None:
    """Port of git.mjs detectDefaultBranch: origin/HEAD symbolic ref, then the
    main/master/trunk candidates (remote preferred, then local). Returns a ref
    resolvable by rev-parse, or None (caller fails closed)."""
    rc, out = git(cwd, ["symbolic-ref", "refs/remotes/origin/HEAD"])
    if rc == 0 and out.strip().startswith("refs/remotes/origin/"):
        return "origin/" + out.strip()[len("refs/remotes/origin/"):]
    for cand in ("main", "master", "trunk"):
        if git(cwd, ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{cand}"])[0] == 0:
            return f"origin/{cand}"
        if git(cwd, ["show-ref", "--verify", "--quiet", f"refs/heads/{cand}"])[0] == 0:
            return cand
    return None


def current_branch(cwd) -> str:
    rc, out = git(cwd, ["branch", "--show-current"])
    return out.strip() or "HEAD"


def working_tree_state(cwd) -> dict:
    """staged / unstaged / untracked file lists + is_dirty (port of git.mjs).

    -z gives NUL-delimited, never-C-quoted paths so a non-ASCII untracked name
    (which core.quotePath would otherwise return as "caf\303\251.py") matches
    the real file in the build_review_tree untracked loop instead of being
    silently excluded from the virtual review tree."""
    def lines(args):
        rc, out = git(cwd, args)
        return [x for x in out.split("\0") if x] if rc == 0 else []
    staged = lines(["diff", "--cached", "--name-only", "-z"])
    unstaged = lines(["diff", "--name-only", "-z"])
    untracked = lines(["ls-files", "--others", "--exclude-standard", "-z"])
    return {
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "is_dirty": bool(staged or unstaged or untracked),
    }


def rev_parse(cwd, ref) -> str | None:
    rc, out = git(cwd, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if rc != 0:
        return None
    return out.strip() or None


ZERO_SHA = "0" * 40


def resolve_base_sha(cwd, base_ref: str, pushed_remote_sha: str | None = None) -> str | None:
    """Range rules for base selection (D8 'Range rules'). Returns a base commit
    sha, or None when it can't be determined (caller fails closed).

    * explicit base_ref (not "auto")  → that ref's sha
    * pre-push with a real remote sha → that sha (the exact published range)
    * on the default branch           → remote/local default tip
    * branch with an upstream         → upstream tip
    * new branch (no upstream)        → merge-base(HEAD, default)
    """
    if base_ref and base_ref != "auto":
        return rev_parse(cwd, base_ref)
    if pushed_remote_sha and pushed_remote_sha != ZERO_SHA:
        return pushed_remote_sha
    default = detect_default_branch(cwd)
    cur = current_branch(cwd)
    default_name = default.split("/")[-1] if default else None
    if default and cur == default_name:
        # Direct-push-to-default: base is the remote default tip.
        return rev_parse(cwd, default)
    up = rev_parse(cwd, "@{upstream}")
    if up:
        return up
    if default:
        rc, out = git(cwd, ["merge-base", "HEAD", default])
        if rc == 0 and out.strip():
            return out.strip()
    return None


def build_review_tree(cwd, review_globs, ignore_globs) -> tuple[str | None, list[str]]:
    """Build the *virtual review tree* (HEAD + staged + unstaged +
    untracked-in-scope) in a throwaway index and return its tree sha (D8
    'virtual review tree'). When the tree is clean this equals HEAD's tree, so
    a Stop-time review of uncommitted work hashes identically to the later
    committed tree when the content is unchanged (D4).

    Returns (tree_sha, skipped_untracked). Untracked files over
    MAX_UNTRACKED_BYTES (git.mjs cap) are excluded and recorded.
    """
    head = rev_parse(cwd, "HEAD")
    with tempfile.NamedTemporaryFile(prefix="mg-index-", delete=False) as tf:
        idx = tf.name
    env = {"GIT_INDEX_FILE": idx}
    try:
        if head:
            if git(cwd, ["read-tree", "HEAD"], env=env)[0] != 0:
                return None, []
        # Stage all tracked modifications + deletions (scope-filtered at diff).
        git(cwd, ["add", "-u"], env=env)
        # add -u only touches paths already in the temp index (= HEAD); a newly
        # git-added (staged, uncommitted) file is in neither that nor the
        # ls-files --others loop below, so pull it in from the real index here
        # (Finding 5). --cached vs HEAD finds staged-new paths; we then stage
        # their working-tree content (consistent with add -u). -z gives NUL-
        # delimited, never-C-quoted paths so non-ASCII staged-new names (which
        # core.quotePath would otherwise return as "caf\303\251.py") match the
        # real file in `git add -- <f>` instead of being silently excluded.
        rc, added = git(cwd, ["diff", "--cached", "--name-only",
                              "--diff-filter=A", "-z"])
        if rc == 0:
            for f in [x for x in added.split("\0") if x]:
                if in_scope(f, review_globs, ignore_globs):
                    git(cwd, ["add", "--", f], env=env)
        skipped: list[str] = []
        state = working_tree_state(cwd)
        for f in state["untracked"]:
            if not in_scope(f, review_globs, ignore_globs):
                continue
            ap = Path(cwd) / f
            try:
                st = ap.lstat()
            except OSError:
                # broken symlink / unreadable — skip (git.mjs edge case)
                continue
            # Regular files only; directories never appear in untracked output
            # but guard anyway. Symlinks are added by git as link blobs.
            if st.st_size > MAX_UNTRACKED_BYTES and not os.path.islink(ap):
                skipped.append(f)
                continue
            git(cwd, ["add", "--", f], env=env)
        rc, out = git(cwd, ["write-tree"], env=env)
        if rc != 0:
            return None, skipped
        return out.strip(), skipped
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


def _diff_between(cwd, base_sha, tree_ish, review_globs, ignore_globs, skipped=None) -> dict:
    """Compute the canonical diff between base_sha and tree_ish (a tree/commit
    sha). An unreadable diff (base/tip object missing locally, git error) returns
    diff_error=True so callers fail closed (Finding 4) instead of treating it as
    an empty no-op diff."""
    # -z gives NUL-delimited, never-C-quoted paths. Without it, git's default
    # core.quotePath returns a non-ASCII path as a C-quoted literal
    # ("caf\303\251.py"); that literal then fails to match the real file when
    # fed back as a `git diff -- <path>` pathspec, silently dropping the file's
    # content from the canonical (hashed, reviewed) diff (Finding 5 residual).
    rc, names = git(cwd, ["diff", "--name-only", "-z", base_sha, tree_ish])
    if rc != 0:
        return {"diff": b"", "diff_hash": _hash(b""), "changed_files": [],
                "skipped_untracked": skipped or [], "tree_sha": tree_ish,
                "diff_error": True}
    changed = [f for f in names.split("\0") if f]
    changed = [f for f in changed if in_scope(f, review_globs, ignore_globs)]
    changed.sort()
    if not changed:
        diff = b""
    else:
        rc, diff = git_bytes(cwd, ["diff", *DIFF_FLAGS, base_sha, tree_ish, "--", *changed])
        if rc != 0:
            return {"diff": b"", "diff_hash": _hash(b""), "changed_files": [],
                    "skipped_untracked": skipped or [], "tree_sha": tree_ish,
                    "diff_error": True}
    return {"diff": diff, "diff_hash": _hash(diff), "changed_files": changed,
            "skipped_untracked": skipped or [], "tree_sha": tree_ish,
            "diff_error": False}


def canonical_diff(cwd, base_sha, review_globs, ignore_globs) -> dict:
    """The single shared canonical diff (D4). Returns:
        {diff: bytes, diff_hash: str, changed_files: [..],
         skipped_untracked: [..], tree_sha: str, diff_error: bool}
    Used identically by produce, the Stop scheduler, and verify — so they
    always agree on the publication diff and its hash.
    """
    tree_sha, skipped = build_review_tree(cwd, review_globs, ignore_globs)
    if tree_sha is None:
        return {"diff": b"", "diff_hash": _hash(b""), "changed_files": [],
                "skipped_untracked": skipped, "tree_sha": None, "diff_error": True}
    return _diff_between(cwd, base_sha, tree_sha, review_globs, ignore_globs, skipped)


def canonical_diff_at_commit(cwd, base_sha, tip_sha, review_globs, ignore_globs) -> dict:
    """Pinned variant for verify (Finding 1): diff base_sha against the PUSHED
    commit, not the working tree. pre-push passes --tip-sha <local_sha>; hashing
    the working tree instead can pass a push of an unreviewed commit."""
    resolved = rev_parse(cwd, tip_sha)
    if resolved is None:
        return {"diff": b"", "diff_hash": _hash(b""), "changed_files": [],
                "skipped_untracked": [], "tree_sha": None, "diff_error": True}
    return _diff_between(cwd, base_sha, resolved, review_globs, ignore_globs, [])


def _hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _asset_sha(path: Path) -> str:
    """Content hash of a producer asset for review_scope_hash (#37 codex:finding-0).
    CONTENT, not path: a checkout-vs-$HOME location difference must NOT churn the
    cache, but DIFFERENT asset content MUST (a `pass` produced under one
    prompt/schema is invalid under another). Stable across tool bumps (D4 — the
    asset file does not change when the codex/claude binary bumps). A missing asset
    hashes to a sentinel rather than raising, so verify stays deterministic (the
    sentinel never matches a real produce → scope-mismatch → stale → fail toward
    re-review, the safe direction) instead of crashing the fast path on a broken
    install."""
    try:
        return _hash(path.read_bytes())
    except OSError:
        return "missing"


def review_scope_hash(cfg: Config) -> str:
    """Compose review_scope_hash (D8). A different reviewer set, glob set,
    severity set, diff algorithm, validator/prompt contract, or per-reviewer
    args is a different review and MUST invalidate the cache (ADR-0010). Never
    includes volatile binary versions (D4)."""
    components = {
        "review_globs": cfg.review_globs,
        "ignore_globs": cfg.ignore_globs,
        "blocking_severities": sorted(cfg.blocking_severities),
        "reviewers": cfg.reviewers,
        "canonical_diff_algo": CANONICAL_DIFF_ALGO_VERSION,
        "validator_contract": VALIDATOR_CONTRACT_VERSION,
        "adversarial_prompt": ADVERSARIAL_PROMPT_VERSION,
        # #37 codex:finding-0 — producer-asset CONTENT identity. Now that assets
        # resolve from the producer's own checkout (resolve_claude_dir), a `pass`
        # cached under one checkout's prompt/schema must NOT be reused under
        # different assets. Content (not path) so a checkout-vs-$HOME relocation
        # does not churn the cache, and a tool bump (asset content unchanged) stays
        # D4-clean. Complements the manual `adversarial_prompt` version above
        # (manual = force a bump without editing the file; sha = auto-bust on any
        # content change, incl. the schema, which has no manual version).
        "adversarial_prompt_sha": _asset_sha(ADVERSARIAL_PROMPT_PATH),
        "schema_sha": _asset_sha(SCHEMA_PATH),
        # #37 CONTENT identity, extended to the validator agent — but rooted at
        # the agent's EXECUTION source ($HOME, see VALIDATOR_AGENT_PATH), not
        # CLAUDE_DIR, since the headless validator loads it from ~/.claude. Its
        # frontmatter `model:` is the EFFECTIVE validator model whenever the
        # `validator_model` pin below is unset — so a default bump (e.g.
        # sonnet→opus) changes which model judges yet leaves `validator_model` at
        # None. Hashing the executed agent's content closes that gap (the #47
        # "change any model setting → re-review" rule must hold for the frontmatter
        # fallback too) and auto-busts on any output_contract edit, complementing
        # the manual VALIDATOR_CONTRACT_VERSION above.
        "validator_agent_sha": _asset_sha(VALIDATOR_AGENT_PATH),
        "reviewer_args": {r: cfg.reviewer_args(r) for r in cfg.reviewers},
        # A different reviewer binary or custom command is a different reviewer
        # implementation and MUST invalidate the cache (one-time bust on upgrade).
        "reviewer_bin": {r: cfg.reviewer_bin(r) for r in cfg.reviewers},
        "reviewer_cmd": {r: cfg.reviewer_cmd(r) for r in cfg.reviewers},
        # #47: ALL four model knobs enter the hash — one rule, "change any
        # model setting → re-review". The dispatcher is pipeline, not judge
        # (CONTEXT.md `validator`), but the scope hash identifies the produce
        # PIPELINE, not just the verdict; excluding it would buy a rare cache
        # hold at the cost of a permanent exception rule. Adding these keys is
        # itself a one-time global bust on upgrade (accepted — #37 precedent).
        "reviewer_model": {r: cfg.reviewer_model(r) for r in cfg.reviewers},
        "validator_model": cfg.validator_model,
        "validator_dispatcher_model": cfg.validator_dispatcher_model,
        # #48: reasoning-effort knobs follow the same one rule.
        "reviewer_reasoning_effort": {r: cfg.reviewer_reasoning_effort(r)
                                      for r in cfg.reviewers},
        "validator_dispatcher_effort": cfg.validator_dispatcher_effort,
    }
    blob = json.dumps(components, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return _hash(blob)


# --------------------------------------------------------------------------
# G1 — Normalize Codex `--json` JSONL → single-doc `.result.findings[]`.
# The "Normalize Codex JSONL" step, ported from the since-removed GHA
# workflow's jq step (ADR-0021; ADR-0012). The output shape MUST stay
# byte-equivalent to what `/run-codex-validators --codex-json` expects
# (test_merge_gate_local keeps the original jq expression verbatim as the
# reference oracle). Five branches, in the same order:
#   codex-failed / missing-result / malformed-payload / normalize-failed / ok
# Skipping this reintroduces #18's silent zero-findings.
# --------------------------------------------------------------------------
def _fallback(status: str, summary: str, extra: dict | None = None) -> dict:
    codex = {"status": status}
    if extra:
        codex.update(extra)
    return {
        "result": {"verdict": "unknown", "summary": summary,
                   "findings": [], "next_steps": []},
        "codex": codex,
    }


def normalize_codex_jsonl(raw: str, codex_exit: int) -> dict:
    """Return the normalized single-doc payload. Mirrors the jq branch order."""
    # Branch 1 — codex-failed (CLI exited non-zero).
    if codex_exit != 0:
        return _fallback(
            "codex-failed",
            f"Codex exited with status {codex_exit}. See codex-review.stderr in the run artefact.",
            {"exit": codex_exit},
        )
    # Parse JSONL; find the LAST item.completed[agent_message] event.
    last_msg = None
    parse_ok = True
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            # A single bad line mid-stream is tolerated by jq -s only if the
            # WHOLE slurp parses; a raw non-JSONL file trips normalize-failed.
            parse_ok = False
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                last_msg = item
    # Branch 5 (outer) — raw was not parseable JSONL at all and yielded nothing.
    if last_msg is None and not parse_ok and raw.strip():
        return _fallback(
            "normalize-failed",
            f"Normalize step could not parse Codex output (codex_exit={codex_exit}). "
            "See codex-review.stderr and normalize.stderr in the artefact.",
            {"exit": codex_exit},
        )
    # Branch 2 — missing-result (no agent_message event).
    if last_msg is None:
        return _fallback(
            "missing-result",
            "Codex returned no agent_message event in its JSONL stream.",
        )
    text = last_msg.get("text")
    payload = None
    if isinstance(text, str):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    # Branch 3 — malformed-payload (agent_message.text not valid JSON).
    if not isinstance(payload, dict):
        return _fallback(
            "malformed-payload",
            "Codex agent_message.text was not valid JSON conforming to review-output.schema.json.",
        )
    # Branch 4 — ok, but only if `findings` is the expected list shape. A custom
    # reviewer's stdout is not --output-schema-constrained, so a dict with a
    # non-list `findings` must fall back to malformed-payload rather than crash
    # _namespace_findings (dict(f) per element) later in produce (M2).
    if not isinstance(payload.get("findings"), list):
        return _fallback(
            "malformed-payload",
            "Codex agent_message.text parsed but `findings` was not a list.",
        )
    return {"result": payload, "codex": {"status": "ok"}}


# --------------------------------------------------------------------------
# Claude reviewer adapter (ADR-0010/0012 — #32). Claude's `claude -p
# --output-format json` envelope is NOT Codex's `item.completed`/`agent_message`
# JSONL event stream: it is a single `{"type":"result", ..., "result": <str>}`
# object. The schema-conforming findings land in that envelope's `result` field
# (a string), steered by `--json-schema` + the JSON-only system directive in
# default_reviewer_runner. On the installed claude (2.1.x) `--json-schema` is a
# SOFT steer — there is no constrained-decoding `structured_output` envelope
# field and `result` is not guaranteed schema-strict (verified empirically,
# AC#2) — so the adapter parses `result` defensively and maps every failure mode
# onto the SAME five-status taxonomy the Codex adapter uses (codex-failed /
# missing-result / malformed-payload / normalize-failed / ok), so a Claude
# reviewer failure fails CLOSED (build_summary F3 → verdict=error), never a
# silent 0-findings pass (#18/#24 lineage). The `codex` wrapper key is the
# historical, reviewer-agnostic status marker produce/build_summary read; the
# validator consumes only `.result.findings[]` and ignores it.
# --------------------------------------------------------------------------
def _strip_json_code_fence(s: str) -> str:
    """Strip a leading/trailing markdown code fence (```json … ```), if present.
    The JSON-only directive forbids fences, but `--json-schema` is a SOFT steer so
    the reviewer occasionally wraps its JSON object in one (observed on claude
    2.1.159, esp. on multi-turn runs). A fenced-but-VALID review must still parse,
    not fail closed as malformed-payload (a false over-block that injects #31 noise).
    Anything that is not a clean fence is returned UNCHANGED, so genuinely non-JSON
    output (prose) still fails closed downstream."""
    t = s.strip()
    if not t.startswith("```"):
        return s
    nl = t.find("\n")                       # drop the opening ``` / ```json line
    if nl == -1:
        return s
    inner = t[nl + 1:]
    if inner.rstrip().endswith("```"):      # drop the closing fence
        inner = inner.rstrip()[:-3]
    return inner


def normalize_claude_json(raw: str, claude_exit: int) -> dict:
    """Normalize the Claude reviewer's `claude -p --output-format json` envelope
    to the shared `.result.findings[]` contract + five-status taxonomy."""
    # Branch 1 — reviewer process failed (CLI exited non-zero).
    if claude_exit != 0:
        return _fallback(
            "codex-failed",
            f"Claude reviewer exited with status {claude_exit}. "
            "See reviewer.stderr in the artefact.",
            {"exit": claude_exit},
        )
    # Parse the result envelope.
    try:
        env = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        env = None
    if not isinstance(env, dict):
        # Branch 5 — non-empty but unparseable; or empty → no review happened.
        if raw.strip():
            return _fallback(
                "normalize-failed",
                "Could not parse the Claude reviewer's --output-format json "
                "envelope. See reviewer.stderr in the artefact.")
        return _fallback(
            "missing-result",
            "Claude reviewer produced no output (empty stdout).")
    # An errored turn (max-turns, exec error, is_error) is a reviewer failure —
    # never trust a partial/aborted turn as a clean 0-findings review. is_error
    # is checked for TRUTHINESS, not identity with True: a malformed envelope
    # carrying is_error as 1/"true"/etc. must still fail closed (the only cost is
    # is_error="false" -> over-block, the fail-safe direction).
    if env.get("is_error") or env.get("subtype") not in ("success", None):
        return _fallback(
            "codex-failed",
            f"Claude reviewer turn errored "
            f"(subtype={env.get('subtype')}, is_error={env.get('is_error')}).",
            {"subtype": env.get("subtype")})
    # Prefer a constrained `structured_output` object if a future claude version
    # supplies one (forward-compatible); else parse the free-form `result`.
    payload = env.get("structured_output")
    if not isinstance(payload, dict):
        result = env.get("result")
        if isinstance(result, dict):
            payload = result
        elif isinstance(result, str) and result.strip():
            try:
                payload = json.loads(_strip_json_code_fence(result))
            except json.JSONDecodeError:
                payload = None
        else:
            payload = None
    # Branch 2 — the envelope carried no usable review payload at all.
    if payload is None and not isinstance(env.get("result"), str):
        return _fallback(
            "missing-result",
            "Claude envelope carried no `result`/`structured_output` payload.")
    # Branch 3 — the reviewer replied (prose / non-object), not schema JSON.
    if not isinstance(payload, dict):
        return _fallback(
            "malformed-payload",
            "Claude `result` was not JSON conforming to review-output.schema.json "
            "(the reviewer may have replied in prose).")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return _fallback(
            "malformed-payload",
            "Claude `result` parsed but `findings` was not a list.")
    # #32 review C1 (fail-OPEN, the #18/#24 class): `--json-schema` is a SOFT
    # steer, so a reply like {"findings": ["critical: SQLi at x"]} parses with
    # `findings` a LIST of NON-OBJECTS. _namespace_findings would SKIP every
    # non-dict element, silently turning a malformed review into an ok 0-findings
    # PASS. Any non-object element means the reviewer did not emit conforming
    # finding objects → malformed-payload (fail CLOSED); never trust a
    # partial/garbled review as clean.
    if any(not isinstance(f, dict) for f in findings):
        return _fallback(
            "malformed-payload",
            "Claude `result.findings[]` contained a non-object element "
            "(the soft schema did not yield conforming finding objects).")
    return {"result": payload, "codex": {"status": "ok"}}


def normalize(reviewer: str, raw: str, exit_code: int) -> dict:
    """Per-reviewer normalize dispatch (#32, ADR-0012). The Codex adapter parses
    Codex's JSONL event stream; the Claude adapter parses Claude's `claude -p`
    result envelope. Both reach the same `.result.findings[]` contract and the
    same five-status taxonomy. A custom-`cmd` reviewer (neither codex nor claude)
    is assumed to emit Codex-shaped JSONL on stdout — the existing seam
    behaviour — so it routes through the Codex adapter."""
    if reviewer == "claude":
        return normalize_claude_json(raw, exit_code)
    return normalize_codex_jsonl(raw, exit_code)


def _reviewer_model(reviewer: str, raw: str) -> str | None:
    """Best-effort actual-model extraction for measurement (#31, AC#8). Claude's
    model is in the envelope's `modelUsage` map; Codex's `--version` does not
    expose the model and its JSONL is not relied on for it (returns None — the
    configured `--model` reviewer_arg, if any, is the operator-visible record)."""
    if reviewer != "claude":
        return None
    try:
        env = json.loads(raw)
    except Exception:
        return None
    if not isinstance(env, dict):
        return None
    mu = env.get("modelUsage")
    if isinstance(mu, dict) and mu:
        # #32 review L2: pick the model that actually PRODUCED the review (most
        # output tokens), not sorted()[0] — alphabetical order would record a
        # background `claude-haiku-*` over the primary `claude-opus-*` when the
        # envelope carries more than one model. Ties break alphabetically (iterate
        # sorted keys) for determinism. A #31 model-provenance input (AC#8).
        def _out_tokens(name: str) -> int:
            v = mu.get(name)
            return v.get("outputTokens", 0) if isinstance(v, dict) else 0
        return max(sorted(mu.keys()), key=_out_tokens)
    m = env.get("model")
    return m if isinstance(m, str) else None


def _model_from_args(args: list[str]) -> str | None:
    """The `--model`/`-m` value from a raw args list, else None. Mirrors
    _unsafe_reviewer_arg's `key[=val]` / `key val` handling for the model flag
    only. Shared by the provenance record below and the runners' model-key/args
    conflict checks (#47)."""
    i, n = 0, len(args)
    while i < n:
        head, eq, inline = str(args[i]).partition("=")
        if head in ("--model", "-m"):
            if eq:
                return inline or None
            return str(args[i + 1]) if i + 1 < n else None
        i += 1
    return None


def _config_key_in_args(args: list[str], key: str) -> bool:
    """True when a codex `-c`/`--config` reviewer_arg sets `key` (#48 — the
    two-writer conflict check for reasoning_effort). Mirrors
    _unsafe_reviewer_arg's `-c key=val` / `-c=key=val` token handling."""
    i, n = 0, len(args)
    while i < n:
        head, eq, inline = str(args[i]).partition("=")
        if head in ("-c", "--config"):
            kv = inline if eq else (str(args[i + 1]) if i + 1 < n else "")
            if kv.split("=", 1)[0].strip() == key:
                return True
            i += 1 if eq else 2
            continue
        i += 1
    return False


def _configured_reviewer_model(cfg: Config, reviewer: str) -> str | None:
    """The operator-configured model for a reviewer, else None — the
    operator-visible model record when the tool output doesn't carry one
    (e.g. Codex). The first-class `model` key (#47) wins over an args-supplied
    `--model`/`-m` (the runners refuse the combination anyway, so precedence
    here only matters for verify-side freshness probes that never refuse)."""
    return cfg.reviewer_model(reviewer) or _model_from_args(cfg.reviewer_args(reviewer) or [])


# --------------------------------------------------------------------------
# summary.json + freshness (D3/D4).
# --------------------------------------------------------------------------
def tuple_dir(artifact_root: Path, base_sha: str, diff_hash: str) -> Path:
    return artifact_root / base_sha / diff_hash


def load_summary(tdir: Path) -> dict | None:
    f = tdir / "summary.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def freshness_state(summary: dict | None, expected_scope_hash: str,
                    freshness_policy: str = "content",
                    current_tools: dict | None = None) -> str:
    """Return one of: 'fresh' | 'missing' | 'scope-mismatch' |
    'schema-incompatible' | 'failing' | 'tool-drift'. verify treats anything but
    'fresh' as stale (AC 'Artefact & freshness'). base_sha/diff_hash matching is
    implied by the tuple-dir lookup, so this checks the remaining hard-gating
    fields.

    Under the default `content` policy, tool-version drift does NOT invalidate
    (a content-identical diff against the same base + scope stays covered across
    tool bumps — D4). Under `tool-strict`, the selected tool versions/models
    (`current_tools`) must also match, so a Codex/Claude bump re-reviews.
    """
    if summary is None:
        return "missing"
    sv = summary.get("schema_version")
    if not isinstance(sv, int) or sv > SCHEMA_VERSION:
        return "schema-incompatible"
    if summary.get("review_scope_hash") != expected_scope_hash:
        return "scope-mismatch"
    if summary.get("verdict") != "pass":
        return "failing"
    if freshness_policy == "tool-strict" and current_tools is not None:
        for key in ("codex_version", "codex_model", "claude_version",
                    "validator_agent_version"):
            if summary.get(key) != current_tools.get(key):
                return "tool-drift"
    return "fresh"


# --------------------------------------------------------------------------
# Bypass trailer (D6).
# --------------------------------------------------------------------------
def tip_bypass_reason(cwd, tip_sha: str, trailer: str) -> str | None:
    """Return the non-empty bypass reason from the tip commit's
    `<trailer>: <reason>` trailer, or None. Honored ONLY under
    client-side-blocking (D6)."""
    fmt = "--format=%(trailers:key=" + trailer + ",valueonly)"
    rc, out = git(cwd, ["log", "-1", fmt, tip_sha])
    if rc == 0 and out.strip():
        return out.strip()
    # Fallback: parse the raw body for older git without %(trailers) key filter.
    rc, body = git(cwd, ["log", "-1", "--format=%B", tip_sha])
    if rc != 0:
        return None
    prefix = f"{trailer}:"
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            reason = line[len(prefix):].strip()
            if reason:
                return reason
    return None


# --------------------------------------------------------------------------
# Producer lock + atomic artefact writes (Scheduler & hooks ACs).
# --------------------------------------------------------------------------
LOCK_TTL_SECONDS = 3600  # a lock older than this is treated as stale (crash)


class ProducerLock:
    """Best-effort cross-process lock so two `produce` runs never collide.
    O_CREAT|O_EXCL lockfile carrying the holder pid + a timestamp; a lock older
    than LOCK_TTL_SECONDS is reclaimed (a crashed producer must not wedge the
    repo forever)."""

    def __init__(self, artifact_root: Path):
        self.path = artifact_root / ".producer.lock"
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if self._stale():
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                return self.__enter__()
            raise LockBusy()
        os.write(fd, f"{os.getpid()} {int(time.time())}".encode())
        os.close(fd)
        self.acquired = True
        return self

    def _stale(self) -> bool:
        try:
            txt = self.path.read_text().split()
            ts = int(txt[1])
        except Exception:
            return True
        return (time.time() - ts) > LOCK_TTL_SECONDS

    def __exit__(self, *exc):
        if self.acquired:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class LockBusy(Exception):
    pass


def write_summary_atomic(tdir: Path, summary: dict) -> None:
    """Write summary.json so verify never reads a half-written file. verify
    reads ONLY summary.json (the sub-dir artefacts are audit-only), so an
    atomic replace of this one file makes the whole tuple appear atomically."""
    tdir.mkdir(parents=True, exist_ok=True)
    tmp = tdir / f".summary.json.tmp-{os.getpid()}"
    tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, tdir / "summary.json")


# --------------------------------------------------------------------------
# Pending-tuple persistence (#33, ADR-0014) — the post-commit producer ↔
# pre-push verify hand-off. The auto-producer hashes its LOCAL base while verify
# resolves the pre-push REMOTE base, so a stale local origin would diverge the
# diff_hash and verify would re-log `missing` (G2); and a push immediately after
# commit can beat the backgrounded produce (G3). The pending tuple
# {base_sha, diff_hash, tip_sha, pid} lets verify (a) trust the producer's base
# for the matched tip and (b) key a bounded wait off the producer's liveness.
# Lives next to the producer lock in artifact_root (gitignored). `(base,
# diff_hash)` is the artefact dedup key; `tip_sha` is verify-match metadata only
# (N1: an amend preserving the diff still matches the wait).
# --------------------------------------------------------------------------
def pending_path(artifact_root: Path) -> Path:
    return artifact_root / ".pending.json"


def read_pending(artifact_root: Path) -> dict | None:
    """The latest pending-produce tuple, or None when absent/unreadable. Never
    raises — a missing or corrupt file just means 'no known in-flight produce'."""
    try:
        return json.loads(pending_path(artifact_root).read_text(encoding="utf-8"))
    except Exception:
        return None


def write_pending(artifact_root: Path, tuple_: dict) -> None:
    """Record the latest pending-produce tuple atomically. The post-commit hook
    writes it at launch (with the detached child's pid); the producer rewrites it
    as it coalesces to a newer committed tip. Best-effort: a write failure must
    never break a commit or a produce."""
    try:
        artifact_root.mkdir(parents=True, exist_ok=True)
        tmp = artifact_root / f".pending.json.tmp-{os.getpid()}"
        tmp.write_text(json.dumps(tuple_) + "\n", encoding="utf-8")
        os.replace(tmp, pending_path(artifact_root))
    except Exception:
        pass


def pid_alive(pid) -> bool:
    """True iff `pid` is a live process this user can signal. `os.kill(pid, 0)`
    raises ProcessLookupError when the pid is dead and PermissionError when it is
    alive but owned by another user (treated as alive — fail toward 'wait', not a
    premature `missing`). A None/garbage pid is dead by construction."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# Adversarial prompt rendering (inline-pinned diff — ADR-0012).
# --------------------------------------------------------------------------
def render_adversarial_prompt(cd: dict, user_focus: str) -> str:
    template = ADVERSARIAL_PROMPT_PATH.read_text(encoding="utf-8")
    n = len(cd["changed_files"])
    target = f"local merge-gate review of {n} changed file(s)"
    review_input = cd["diff"].decode("utf-8", "replace")
    if cd["skipped_untracked"]:
        review_input += "\n\n[NOTE] Untracked files skipped (over size cap): " + \
            ", ".join(cd["skipped_untracked"])
    # Inline-evidence guidance — the diff is pinned below; never self-collect.
    guidance = "Use the repository context below as primary evidence."
    return (template
            .replace("{{TARGET_LABEL}}", target)
            .replace("{{USER_FOCUS}}", user_focus or "(none)")
            .replace("{{REVIEW_COLLECTION_GUIDANCE}}", guidance)
            .replace("{{REVIEW_INPUT}}", review_input))


# --------------------------------------------------------------------------
# Default reviewer + validator runners (the real subprocess calls). Tests
# inject fakes so the reviewer-set seam (loop / namespacing / union /
# per-reviewer sub-dirs) is exercised without real codex/claude.
# --------------------------------------------------------------------------
# reviewer_args is a narrow escape hatch for benign codex tuning (model,
# reasoning effort). It must NOT relax the local profile's one hard security
# invariant — the reviewer runs read-only-sandboxed and is never bypassed
# (ADR-0012). An ALLOWLIST is used, not a denylist: codex has many flags that
# silently widen the read-only sandbox WITHOUT conflicting with our
# `--sandbox read-only` (e.g. `--add-dir` grants extra writable dirs; `-p`/
# `--profile` loads a profile that can set sandbox_mode/approval_policy/
# shell_environment_policy, none of which our CLI `--sandbox` pins except
# sandbox_mode; `--enable`, `--ignore-user-config`, `--ignore-rules`), so a
# denylist is unmaintainable — anything not provably sandbox-neutral is refused.
# (A duplicate `--sandbox` is a clap hard-error, so that one fails closed on its
# own; the live risk is the additive/profile flags above.)
_ALLOWED_REVIEWER_FLAGS = {"--model", "-m"}        # value-taking, sandbox-neutral
# `-c key=val` config keys that may NOT be set via reviewer_args. Sandbox/approval/
# env/permission/trust keys relax the read-only posture (the hardcoded CLI
# `--sandbox read-only` currently wins over -c, but we refuse them anyway so the
# guard does not silently depend on codex's flag-vs-config precedence). `mcp` and
# `model_provider`/`model_providers` are an INTEGRITY guard, not sandbox: a
# redirected model endpoint could forge the review ("no findings" → gate passes
# unreviewed code) or exfiltrate the diff. The operator's own global codex config
# still applies — this only blocks the per-reviewer override surface.
_DENIED_CONFIG_PREFIXES = ("sandbox", "approval", "shell_environment_policy",
                           "features", "trust", "mcp", "permission",
                           "default_permissions", "writable", "network",
                           "model_provider")


def _unsafe_reviewer_arg(extra: list[str]) -> str | None:
    """Return the first reviewer_args token we cannot prove is sandbox-neutral,
    else None (M1). Allowlist: only `--model`/`-m` and benign `-c key=val` whose
    key is outside _DENIED_CONFIG_PREFIXES pass; everything else is refused."""
    i, n = 0, len(extra)
    while i < n:
        t = str(extra[i]).strip()
        head, eq, inline = t.partition("=")
        if head in ("-c", "--config"):
            kv = inline if eq else (extra[i + 1] if i + 1 < n else "")
            key = kv.split("=", 1)[0].strip().lower()
            if any(key.startswith(p) for p in _DENIED_CONFIG_PREFIXES):
                return t
            i += 1 if eq else 2
            continue
        if head in _ALLOWED_REVIEWER_FLAGS:
            i += 1 if eq else 2          # consume the flag's value token too
            continue
        return t                          # unknown / non-allowlisted → refuse
    return None


# The Claude reviewer's reviewer_args are appended after `--permission-mode
# bypassPermissions`; claude flags like --mcp-config / --add-dir / --settings /
# --setting-sources load tool, file, or config surfaces the read-only
# `--disallowedTools` denylist cannot fully cover (an MCP tool has an arbitrary
# name the denylist can't enumerate). Parity with the Codex M1 guard demands we
# refuse any claude reviewer_arg that is not provably read-only-neutral. Only
# --model/-m is allowlisted — there is no documented claude reviewer need beyond
# model selection — so anything else fails closed (#32 review C2). NOTE: this is
# STRICTER than _unsafe_reviewer_arg and intentionally separate: claude has no
# benign `-c key=val` surface (claude `-c` is `--continue`, not codex config).
_CLAUDE_ALLOWED_REVIEWER_FLAGS = {"--model", "-m"}


def _unsafe_claude_reviewer_arg(extra: list[str]) -> str | None:
    """Return the first claude reviewer_args token outside the allowlist
    (--model/-m, value-taking), else None."""
    i, n = 0, len(extra)
    while i < n:
        head, eq, _ = str(extra[i]).partition("=")
        if head in _CLAUDE_ALLOWED_REVIEWER_FLAGS:
            i += 1 if eq else 2          # consume the flag's value token too
            continue
        return str(extra[i])             # unknown / non-allowlisted → refuse
    return None


def _killpg(pid: int) -> None:
    """SIGKILL the whole process group led by `pid`; tolerate an already-dead
    child/group. `start_new_session=True` makes pgid == pid, and the group
    outlives a dead leader as long as any descendant remains — so this reaps the
    orphans even after the direct child is gone."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run_reaped(*popenargs, input=None, capture_output=False, timeout=None,
                **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run`-equivalent that reaps the child's WHOLE process group on
    timeout. The headless `claude -p` reviewer/validator spawns descendants (tool
    Bash, MCP servers, a wedged turn's hung child). Plain `subprocess.run(timeout=)`
    SIGKILLs only the DIRECT child; descendants are reparented to init and leak —
    and on the very timeout that fires (a genuine wedge) the hung descendant leaks
    INDEFINITELY rather than self-completing. `start_new_session=True` puts the
    child in its own process group; on timeout we `killpg` the group so nothing
    outlives the bound. Same fail-closed contract as run(): returns
    CompletedProcess, re-raises TimeoutExpired (callers' `except TimeoutExpired`
    handlers are unchanged)."""
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    with subprocess.Popen(*popenargs, start_new_session=True, **kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid)
            proc.communicate()  # group is dead → pipes hit EOF, drains promptly
            raise
        except BaseException:  # incl. KeyboardInterrupt — don't leak the group
            _killpg(proc.pid)
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _clear_stale_reviewer_artefacts(sub_dir: Path) -> None:
    """Pre-clear the reviewer's raw-output sinks (`reviewer.stdout`/`.stderr`)
    before a (re)run, mirroring default_validator_runner's validators.{json,md}
    clear (#15/C1, L1440). #34 made reviewer.stdout the #31 reviewer-reliability
    evidence sink, but BOTH sinks are written ONLY on the subprocess-return path:
    the unsafe-arg refusal / timeout / exception early returns write neither, and
    produce mkdir's sub_dir with exist_ok and never clears it. So without this,
    a same-tuple re-produce whose rerun fails early would leave the PRIOR
    successful run's bytes in sub_dir, and an auditor reading a codex-failed /
    timeout row would mis-attribute stale evidence from a different, successful
    run (#38). Called at the TOP of each runner — strictly upstream of every
    early return, so even an unsafe-arg refusal clears first. After the clear,
    any sink present afterward was written THIS run; a MISSING file is the
    unambiguous "no output captured this run" signal.

    `unlink(missing_ok=True)`: the ONLY benign failure is "already absent" (the
    desired post-state). Any OTHER OSError (e.g. EPERM on a read-only sub_dir)
    must PROPAGATE — a stale file we failed to remove must never be silently
    presented as this-run evidence (the very invariant this clear upholds). The
    raise aborts the runner → produce errors → next verify sees `missing`, never a
    stale artefact read as fresh (claude:finding-1, #38 dogfood — stricter than the
    `default_validator_runner` precedent's swallow-all `except OSError`)."""
    for stale in (sub_dir / "reviewer.stdout", sub_dir / "reviewer.stderr"):
        stale.unlink(missing_ok=True)


def default_reviewer_runner(name: str, cfg: Config, cd: dict, sub_dir: Path,
                            cwd: Path, user_focus: str) -> tuple[str, int]:
    """Invoke one reviewer; return (jsonl_stdout, exit_code). The Codex reviewer
    is `codex exec --json --output-schema <schema> --sandbox read-only` with the
    canonical diff fed INLINE via stdin (ADR-0012) — never `codex review`, never
    model self-collection of the diff."""
    # #38: pre-clear the raw sinks at the TOP so EVERY early return below (the
    # codex unsafe-arg refusal, the custom-cmd RuntimeError, timeout, exception)
    # is downstream of it — a failed rerun then leaves no stale stdout/stderr.
    # Covers the claude path too: this runs before the `name == "claude"`
    # delegation, so _run_claude_reviewer's own clear is a defensive no-op there.
    _clear_stale_reviewer_artefacts(sub_dir)
    prompt = render_adversarial_prompt(cd, user_focus)
    if name == "codex":
        extra = cfg.reviewer_args("codex") or []
        bad = _unsafe_reviewer_arg(extra)
        if bad:
            return (f"refusing reviewer_args {bad!r}: not provably sandbox-neutral; "
                    "the local reviewer is read-only-sandboxed by invariant and "
                    "must not be bypassed (M1)", 2)
        model = cfg.reviewer_model("codex")
        # #47: the first-class `model` key and an args `--model`/`-m` are two
        # writers of the same flag — refuse the combination fail-closed rather
        # than letting clap's duplicate-flag handling pick a winner silently.
        if model and _model_from_args(extra):
            return ("refusing codex reviewer config: both the `model` key and a "
                    "`--model`/`-m` reviewer_arg are set — set exactly one (#47)", 2)
        effort = cfg.reviewer_reasoning_effort("codex")
        # #48: same two-writers rule for the effort knob — the args side spells
        # it `-c model_reasoning_effort=<v>`.
        if effort and _config_key_in_args(extra, "model_reasoning_effort"):
            return ("refusing codex reviewer config: both the `reasoning_effort` "
                    "key and a `-c model_reasoning_effort=` reviewer_arg are set "
                    "— set exactly one (#48)", 2)
        cmd = [cfg.reviewer_bin("codex"), "exec", "--json",
               "--output-schema", str(SCHEMA_PATH),
               "--sandbox", "read-only", "--skip-git-repo-check",
               "-C", str(cwd)]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        if extra:
            cmd += extra
    elif name == "claude":
        return _run_claude_reviewer(name, cfg, prompt, sub_dir, cwd)
    else:
        spec = cfg._d.get("reviewer_config", {}).get(name, {})
        cmd = spec.get("cmd")
        if not cmd:
            raise RuntimeError(
                f"reviewer {name!r} has no Codex builtin and no `cmd` in "
                f"[merge-gate.local.producer.{name}]")
    try:
        # _run_reaped (not bare subprocess.run) with a hard timeout so a wedged
        # `codex exec` (or a custom reviewer) fails closed and its whole process
        # group is reaped, never hanging produce indefinitely (#30 follow-up,
        # parity with the claude reviewer/validator sites).
        p = _run_reaped(cmd, cwd=str(cwd), input=prompt.encode("utf-8"),
                        capture_output=True, timeout=_CODEX_REVIEWER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return (f"{name} reviewer timed out after {_CODEX_REVIEWER_TIMEOUT_S}s "
                "(fail-closed)", 124)
    except Exception as e:
        return f"reviewer invocation failed: {e}", 127
    (sub_dir / "reviewer.stderr").write_bytes(p.stderr)
    # #34: persist the RAW reviewer stdout (bytes, before normalize) so a
    # malformed-payload / codex-failed row stays auditable — the normalized
    # _fallback summary alone can't tell prose from prose-preamble-then-JSON.
    # #38: written only HERE, on the subprocess-return path; the unsafe-arg
    # refusal / timeout / exception early returns above write NEITHER sink. The
    # top-of-runner pre-clear is what makes a MISSING file mean "no output
    # captured this run" (not stale bytes from a prior produce of the same tuple);
    # an empty-but-present file means the subprocess returned empty stdout.
    (sub_dir / "reviewer.stdout").write_bytes(p.stdout)
    return p.stdout.decode("utf-8", "replace"), p.returncode


# The Claude reviewer is read-only-sandboxed by invariant (ADR-0012 §3, parity
# with codex `--sandbox read-only`). The SOLE primary enforcement is a POSITIVE
# TOOL ALLOWLIST via `--tools` (only these built-in tools EXIST in the session;
# the diff is fed INLINE so Read/Grep/Glob are for optional read-only grounding).
# It replaces the prior `--disallowedTools`-only denylist, which the #32 post-merge
# review showed did NOT match the installed claude's headless toolset: empirically
# (claude 2.1.159) `Workflow, ScheduleWakeup, Skill, ToolSearch, AskUserQuestion`
# SURVIVED that denylist while ~12 denied names were phantoms (not exposed
# headless). A positive allowlist cannot silently drift — under `--tools
# Read,Grep,Glob`, Bash/Write/Workflow are absent BY CONSTRUCTION (verified live:
# a Bash attempt returns "no such tool"), and any future tool is excluded too.
#
# IMPORTANT: `--permission-mode dontAsk` is NOT a deny layer. Verified live (claude
# 2.1.159, settings isolated, no `--tools`) it AUTO-APPROVES whatever tools exist
# and runs Bash — it only suppresses the interactive prompt; it does not deny. So
# `--tools` is the ONE thing standing between the reviewer and full tool access; if
# it ever regressed (dropped, or a claude version stops honoring it) the posture
# would fail OPEN. `_CLAUDE_REVIEWER_DENIED_TOOLS` is therefore a DEFENSE-IN-DEPTH
# second layer (via `--disallowedTools`, which DOES deny — verified): if `--tools`
# breaks, the denylist still blocks the known mutating/exec/spawn/network/
# persistence tools, so the failure mode stays CLOSED. Completeness matters less
# here than it did as the round-1 sole layer, since `--tools` is the primary gate.
_CLAUDE_REVIEWER_TOOLS = "Read,Grep,Glob"

# Defense-in-depth denylist (#32 re-review): the known non-read tools the installed
# claude exposes headless — directly (Agent, Workflow, ScheduleWakeup, Skill,
# ToolSearch, AskUserQuestion) or loadable via ToolSearch (Cron*, Task*, WebFetch/
# Search, RemoteTrigger, Monitor, PushNotification, NotebookEdit) — plus the obvious
# mutators/exec. Passed via `--disallowedTools` as ONE comma-joined token (variadic,
# but a single token avoids swallowing a trailing reviewer_arg; the comma form was
# verified to deny). SECOND layer behind `--tools`; Read/Grep/Glob are intentionally
# absent here so the allowlist and denylist never conflict.
_CLAUDE_REVIEWER_DENIED_TOOLS = ",".join([
    # mutating / exec
    "Bash", "BashOutput", "KillShell", "Edit", "Write", "MultiEdit", "NotebookEdit",
    # network
    "WebFetch", "WebSearch",
    # spawn / orchestrate / slash (subagents/workflows can re-acquire denied tools)
    "Agent", "Task", "Workflow", "SlashCommand", "Skill",
    # deferred-tool loader (could surface any tool below)
    "ToolSearch",
    # persistence / scheduling / remote / background (outlive the review session)
    "ScheduleWakeup", "RemoteTrigger", "Monitor", "PushNotification",
    "CronCreate", "CronDelete", "CronList",
    "TaskCreate", "TaskStop", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput",
    # interactive (would stall a non-interactive review)
    "AskUserQuestion",
])

# Hard wall-clock bound for the reviewer subprocess (#32 read-only review — the
# review's "no subprocess timeout" finding; `AskUserQuestion` was reachable, which
# in a non-interactive turn could stall produce). `claude -p` has no documented
# client timeout, so an OS-level subprocess timeout is the official approach. On
# timeout the run fails CLOSED (exit 124 → normalize codex-failed → verdict error),
# never a silent pass or an indefinitely-wedged produce.
_CLAUDE_REVIEWER_TIMEOUT_S = 600

# Known tier aliases the Agent tool's `model` param accepts (#47). The
# validator AGENT is spawned via the Agent tool, whose model param is an
# alias enum (observed: sonnet/opus/haiku/fable) — NOT the free-form
# `--model` string the reviewer/dispatcher CLIs take (the sub-agents doc
# claims full IDs too, but the live tool schema is the enum; fail-closed
# keeps the alias set). An off-enum value would fail only deep inside the
# headless dispatch (Agent tool error → validators.json absent → F2 blanket
# over-block), the exact silent-diagnosis class #31 spent sessions on — so
# cmd_produce refuses it loudly up front instead. New tier ships → add one
# entry here. `fable` availability is account-dependent.
_VALIDATOR_MODEL_ALIASES = {"haiku", "sonnet", "opus", "fable"}

# Enum-shaped reasoning-effort knobs (#48), validated loudly at cmd_produce
# (same posture as the validator alias: a typo must not become a deep
# reviewer-failure / F2 over-block diagnosis). Free-form MODEL strings stay
# unvalidated by contrast — their value space is huge and the CLI fails with
# recorded evidence. New level ships → add one token here.
#   codex: developers.openai.com/codex/config-reference (model_reasoning_effort;
#          xhigh is model-dependent, bundled models default to medium)
_CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
#   claude: code.claude.com/docs/en/cli-reference `--effort` (model-dependent:
#           e.g. Sonnet 4.6 has no xhigh — a mismatch fails at the CLI with
#           stderr evidence; this set only catches off-enum typos)
_CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _invalid_validator_model(cfg: Config) -> str | None:
    """Refusal message when `validator.model` is set to a non-alias (#47),
    else None. Unset passes (the agent frontmatter default applies)."""
    m = cfg.validator_model
    if m is None or m in _VALIDATOR_MODEL_ALIASES:
        return None
    allowed = "/".join(sorted(_VALIDATOR_MODEL_ALIASES))
    return (f"refusing validator.model {m!r}: not a known tier alias "
            f"({allowed}) — the validator agent is dispatched via the Agent "
            "tool, which takes an alias, not a full model id (#47)")


def _invalid_reasoning_effort(cfg: Config) -> str | None:
    """Refusal message for the first off-enum reasoning-effort knob (#48),
    else None. Unset keys pass (tool defaults). Only ACTIVE reviewers' keys
    are checked (#48 produce-review, codex:finding-0 + claude:finding-1, both
    upheld): an inert key on a reviewer outside cfg.reviewers must not wedge
    produce — consistent with review_scope_hash, which hashes active
    reviewers' knobs only. The validator dispatcher always runs, so its knob
    is always checked."""
    if "codex" in cfg.reviewers:
        v = cfg.reviewer_reasoning_effort("codex")
        if v is not None and v not in _CODEX_REASONING_EFFORTS:
            return (f"refusing codex reasoning_effort {v!r}: not an official "
                    f"model_reasoning_effort value "
                    f"({'/'.join(sorted(_CODEX_REASONING_EFFORTS))}) (#48)")
    checks = [("validator.dispatcher_effort", cfg.validator_dispatcher_effort)]
    if "claude" in cfg.reviewers:
        checks.insert(0, ("claude reasoning_effort",
                          cfg.reviewer_reasoning_effort("claude")))
    for label, val in checks:
        if val is not None and val not in _CLAUDE_EFFORTS:
            return (f"refusing {label} {val!r}: not an official --effort "
                    f"value ({'/'.join(sorted(_CLAUDE_EFFORTS))}) (#48)")
    return None


# Hard wall-clock bound for the VALIDATOR subprocess (#31 seed finding). The validator
# runs a full subagent (heavier than the reviewer's single turn), so a more generous
# bound; on timeout it fails CLOSED (default_validator_runner returns None →
# build_summary F2 over-blocks), never a silent pass or an indefinitely-wedged produce.
_CLAUDE_VALIDATOR_TIMEOUT_S = 900

# Hard wall-clock bound for the codex (and any custom-`cmd`) reviewer subprocess
# (#30 follow-up — the codex reviewer had NO timeout while the claude sites do; a
# wedged `codex exec` could hang produce indefinitely). Mirrors the claude reviewer
# bound (a single adversarial turn). On timeout the run fails CLOSED (exit 124 →
# reviewer failure → produce verdict error), never a silent pass or wedged produce.
_CODEX_REVIEWER_TIMEOUT_S = 600

# JSON-only directive. On claude 2.1.x `--json-schema` is a SOFT steer (no
# constrained-decoding `structured_output` field; verified — AC#2), so this
# system prompt is what practically forces a parseable JSON object into the
# envelope's `result`, which normalize_claude_json then parses. The Agent SDK
# fallback (AC#2) is unavailable in this environment (not installed), so the CLI
# flag + directive IS the recorded mechanism.
_CLAUDE_REVIEWER_JSON_DIRECTIVE = (
    "You are a non-interactive code reviewer. Respond with ONLY a single JSON "
    "object conforming to the provided review-output schema, and nothing else — "
    "no prose, no markdown, no code fences. Required keys: `verdict` "
    "(\"approve\" or \"needs-attention\"), `summary`, `findings` (each finding "
    "MUST include `severity` (one of critical/high/medium/low), `title`, "
    "`body`, `file`, `line_start`, `line_end`, `confidence` (0..1), and "
    "`recommendation`), and `next_steps`. The JSON object is your entire reply.")


# CLAUDE_CODE_* vars that are auth/provider config, NOT nested-session markers, and
# must survive the strip below — else auth fails and reintroduces the auth-failure
# over-block the strip exists to prevent (#31 seed findings). The strip stays a broad
# prefix match because the session markers are open-ended (CLAUDECODE, _ENTRYPOINT,
# _EXECPATH, _TMPDIR, _SESSION_ID, _SSE_PORT, …); a denylist would miss one and the
# child would no-op again. So auth/provider vars are preserved by ALLOWLIST instead:
#   OAUTH_TOKEN              — headless/CI auth token
#   USE_BEDROCK / USE_VERTEX — provider selection; stripping makes the child fall back
#                              to the direct Anthropic API → auth failure → over-block
#                              for Bedrock/Vertex operators (#31 seed finding :1196).
# Extend here when a new auth/provider var is added.
_PRESERVE_CLAUDE_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
}


def _fresh_claude_session_env(extra: dict) -> dict:
    """Child env for a headless `claude -p` spawned by produce. produce may run
    INSIDE a Claude session (the Stop scheduler launches it from one), and a
    nested `claude` that inherits the parent's CLAUDECODE / CLAUDE_CODE_* session
    markers NO-OPS to EMPTY output (verified — claude self-guards against nested
    sessions). Stripping those markers makes the child run as a fresh top-level
    session so it actually reviews. That fresh session's Stop hook then DOES fire
    — which is exactly why the recursion guard (`extra`'s
    MERGE_GATE_PRODUCER_RUNNING=1) is mandatory: the hook reads it and no-ops
    instead of scheduling a nested produce (#24/#26/#28/#29 runaway). Auth/config
    env (ANTHROPIC_*, PATH, HOME, …) is preserved — INCLUDING CLAUDE_CODE_OAUTH_TOKEN
    (see _PRESERVE_CLAUDE_ENV): it matches the CLAUDE_CODE_ prefix but is auth, not a
    session marker, so stripping it would break token-auth (CI) → over-block."""
    base = {k: v for k, v in os.environ.items()
            if k != "CLAUDECODE"
            and (not k.startswith("CLAUDE_CODE_") or k in _PRESERVE_CLAUDE_ENV)}
    base.update(extra)
    return base


def _run_claude_reviewer(name: str, cfg: Config, prompt: str, sub_dir: Path,
                         cwd: Path) -> tuple[str, int]:
    """The Claude second reviewer (#32, ADR-0010/0012): headless
    `claude -p --output-format json --json-schema <inline schema>` run advisory
    alongside Codex. Returns (stdout, exit_code) for normalize_claude_json.

    Recursion guard (AC#5/#6, the #24/#26/#28/#29 fail-class): the child carries
    MERGE_GATE_PRODUCER_RUNNING=1 (mirroring default_validator_runner). NOTE the
    read-only ISOLATION below (`--setting-sources ""`) means the reviewer child
    loads NO hooks at all, so merge_gate_scheduler.py never fires in it — the guard
    is now belt-and-suspenders for that path, and primarily protects the validator.

    Read-only posture: the `--tools`/`--allowedTools` allowlist is the ACTUAL gate
    (only Read/Grep/Glob exist); `--disallowedTools` is a defense-in-depth second
    layer; `--permission-mode dontAsk` only suppresses prompts (it does NOT deny —
    verified live); `--setting-sources ""` (no hooks/plugins) + `--strict-mcp-config`
    (no MCP) isolate the session. All verified on 2.1.159."""
    # #38: pre-clear the raw sinks at the TOP so the unsafe-arg refusal / timeout
    # / exception early returns below leave no stale stdout/stderr. On the normal
    # claude path this is reached via default_reviewer_runner, which already
    # cleared, so this is a defensive no-op there; it makes a DIRECT call (or a
    # future caller) self-sufficient — parity with default_validator_runner.
    _clear_stale_reviewer_artefacts(sub_dir)
    extra = cfg.reviewer_args("claude") or []
    # M1 parity (#32 review C2): refuse any reviewer_arg that is not provably
    # read-only-neutral BEFORE invoking. The tool allowlist + isolation below cannot
    # cover a flag like --mcp-config/--add-dir/--setting-sources that would re-widen
    # the surface, so only --model/-m is allowlisted.
    bad = _unsafe_claude_reviewer_arg(extra)
    if bad:
        return (f"refusing claude reviewer_args {bad!r}: only --model/-m is "
                "allowlisted; the reviewer is read-only-sandboxed by invariant "
                "and must not load tool/permission/MCP/config surfaces (#32 C2)", 2)
    model = cfg.reviewer_model("claude")
    # #47: same two-writers refusal as the codex runner — the `model` key and
    # an args `--model`/`-m` must not coexist.
    if model and _model_from_args(extra):
        return ("refusing claude reviewer config: both the `model` key and a "
                "`--model`/`-m` reviewer_arg are set — set exactly one (#47)", 2)
    # Inline schema CONTENT, not a path — a path makes `claude -p --json-schema`
    # exit 0 with EMPTY output (verified, AC#2); empty output then fails closed
    # (missing-result → reviewer failure) but never actually reviews. The schema
    # file is ~1.7KB, well under the per-arg limit, so it stays an inline arg.
    schema_content = SCHEMA_PATH.read_text(encoding="utf-8")
    # #32 review C3 (argv MAX_ARG_STRLEN ≈ 128KB): the diff-bearing prompt is fed
    # via STDIN — parity with the Codex runner — NOT as the `-p` argv positional.
    # A large in-scope diff as a single argv element would exceed the per-arg limit,
    # execve would raise E2BIG, and the reviewer would error before running (wedging
    # a client-side-blocking repo on EVERY large PR and injecting error noise into
    # #31). `-p` is the boolean `--print` flag; with no positional, claude reads the
    # prompt from stdin (verified live against claude 2.1.x).
    cmd = [cfg.reviewer_bin("claude"), "-p",
           "--output-format", "json",
           "--json-schema", schema_content,
           "--append-system-prompt", _CLAUDE_REVIEWER_JSON_DIRECTIVE,
           # `dontAsk` runs non-interactively without prompting. NOTE it is NOT a
           # deny layer — verified live (settings isolated, no --tools) it
           # auto-APPROVES available tools and runs Bash. The read-only gate is
           # `--tools` below (+ the `--disallowedTools` defense-in-depth layer).
           "--permission-mode", "dontAsk",
           # Read-only ISOLATION (#32 read-only review): load ZERO settings sources
           # so the operator's ~/.claude hooks (Stop/SessionStart/SessionEnd run
           # SHELL — a status regenerator that rewrites files, a devlog writer) and
           # plugins do NOT fire during a "read-only" review (verified: a sentinel
           # project hook does not fire under `--setting-sources ""`); and ignore
           # all MCP configs. OAuth/keychain auth is the credentials file, not a
           # settings source, so it is unaffected (verified: exit 0 under isolation).
           "--setting-sources", "",
           "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]
    # #48: first-class effort knob (`--effort`, verified to apply in -p mode).
    # No two-writer conflict is possible here: the claude args allowlist only
    # admits --model/-m, so an args-supplied --effort is already refused above.
    effort = cfg.reviewer_reasoning_effort("claude")
    if effort:
        cmd += ["--effort", effort]
    cmd += extra
    # `--tools` is the positive availability allowlist (only these built-in tools
    # exist) — the PRIMARY read-only gate; `--allowedTools` permits those reads under
    # dontAsk so grounding is not auto-denied; `--disallowedTools` is the
    # DEFENSE-IN-DEPTH denylist that still blocks the known dangerous tools if
    # `--tools` ever regresses (dontAsk itself does NOT deny). All three are variadic
    # — pass each as ONE comma-joined token and keep them LAST so a trailing
    # reviewer_arg (extra, e.g. --model) is never swallowed.
    cmd += ["--tools", _CLAUDE_REVIEWER_TOOLS,
            "--allowedTools", _CLAUDE_REVIEWER_TOOLS,
            "--disallowedTools", _CLAUDE_REVIEWER_DENIED_TOOLS]
    env = _fresh_claude_session_env({"MERGE_GATE_PRODUCER_RUNNING": "1"})
    try:
        # Prompt on stdin (C3); claude -p with no positional reads it. Hard timeout
        # so a wedged turn (e.g. a tool that blocks) fails closed, never hangs produce.
        # _run_reaped (not bare subprocess.run) so a timeout SIGKILLs the whole
        # process group — `claude -p` descendants must not outlive the bound.
        p = _run_reaped(cmd, cwd=str(cwd), input=prompt.encode("utf-8"),
                        env=env, capture_output=True,
                        timeout=_CLAUDE_REVIEWER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return (f"claude reviewer timed out after {_CLAUDE_REVIEWER_TIMEOUT_S}s "
                "(fail-closed)", 124)
    except Exception as e:
        return f"claude reviewer invocation failed: {e}", 127
    (sub_dir / "reviewer.stderr").write_bytes(p.stderr)
    # #34: persist the RAW `claude -p` envelope (bytes, before normalize) so a
    # malformed-payload row — e.g. the reviewer drifting to prose in `.result` —
    # stays auditable from disk. #38: written only HERE, on the subprocess-return
    # path; the unsafe-arg refusal / timeout / exception early returns above write
    # NEITHER sink. The top-of-runner pre-clear makes a MISSING file mean "no
    # output captured this run"; an empty-but-present file means empty stdout.
    (sub_dir / "reviewer.stdout").write_bytes(p.stdout)
    return p.stdout.decode("utf-8", "replace"), p.returncode


def default_validator_runner(name: str, findings_json: Path, sub_dir: Path,
                             cwd: Path, intent_file: Path | None,
                             cfg: Config | None = None) -> dict | None:
    """Run the validator in its OWN headless context (#24/#26/#29). Returns the
    parsed validators.json (with `aggregate`) or None on failure. `cfg` (#47)
    carries the two validator model knobs; None (a legacy direct caller) means
    both unset — the tool defaults."""
    # Clear any leftover artefact from a prior produce of the same tuple
    # (produce mkdir's sub_dir with exist_ok=True and never clears it). After
    # this, ANY validators.json present afterward was written THIS run, so a
    # failed rerun (auth / rate-limit / crash before write) leaves no file ->
    # return None -> build_summary F2 fail-safe blocks rather than pairing a
    # STALE dismiss with a NEW critical (C1).
    vj = sub_dir / "validators.json"
    for stale in (vj, sub_dir / "validators.md"):
        try:
            stale.unlink()
        except OSError:
            pass
    slash = (f"/run-codex-validators --codex-json {findings_json} "
             f"--soft-mode false --out-dir {sub_dir}")
    if intent_file is not None:
        slash += f" --intent-from {intent_file}"
    # #47: the validator AGENT's model rides the slash invocation (the skill
    # passes it to the Agent tool's `model:` param) — the skill reads no
    # harness.toml by contract, so the CLI arg is the only carrier. Absent →
    # the agent frontmatter default (`model: sonnet`). cmd_produce already
    # refused non-alias values (_VALIDATOR_MODEL_ALIASES), so this is clean.
    agent_model = cfg.validator_model if cfg is not None else None
    if agent_model:
        slash += f" --agent-model {agent_model}"
    # Strip nested-session markers so the validator's headless `claude -p` runs as a
    # FRESH top-level session and actually executes. produce may run INSIDE a Claude
    # session (the Stop scheduler), and a child inheriting CLAUDECODE / CLAUDE_CODE_*
    # no-ops to EMPTY output (verified — see _fresh_claude_session_env), which
    # build_summary's F2 fail-safe then turns into blanket over-block — silently
    # poisoning #31's validator-discernment/composition measurement. Mirrors the #32
    # reviewer fix. UNLIKE the reviewer we CANNOT isolate settings: the validator
    # dispatches the `/run-codex-validators` SKILL (+ its subagent), loaded FROM the
    # ~/.claude settings sources. `--setting-sources ""` makes the slash command
    # undiscoverable (verified: "Unknown command: /run-codex-validators"); `--bare`
    # skips hooks but "never reads the OAuth keychain" (verified) → breaks this
    # OAuth-only operator's auth. So the validator runs WITH settings, and that fresh
    # session's hooks fire. The runaway path is contained (the Stop scheduler reads
    # MERGE_GATE_PRODUCER_RUNNING=1 and no-ops), but other operator session hooks
    # (status regen / devlog) still run — a residual accepted under the local-profile
    # trust model (self-authored diffs only, ADR-0009), tracked as a #31 seed finding.
    env = _fresh_claude_session_env({"MERGE_GATE_PRODUCER_RUNNING": "1"})
    try:
        # Hard timeout (#31 seed finding): the validator now does real long-running
        # work (a full subagent run, ~175s observed) instead of the prior no-op, so an
        # unbounded subprocess could wedge produce indefinitely. Fail CLOSED on timeout
        # (return None → F2 over-blocks), parity with the reviewer's bound. _run_reaped
        # so the timeout SIGKILLs the whole process group, not just the direct child.
        dispatcher_cmd = ["claude", "-p", slash, "--permission-mode", "bypassPermissions"]
        # #47: the DISPATCHER's own model (orchestration session — pipeline,
        # not judge). Free-form string: `claude -p --model` accepts aliases and
        # full model IDs alike. Absent → the CLI default.
        if cfg is not None and cfg.validator_dispatcher_model:
            dispatcher_cmd += ["--model", cfg.validator_dispatcher_model]
        # #48: dispatcher effort (`--effort`). The validator AGENT has no
        # per-dispatch effort surface (frontmatter-only) — deliberately absent.
        if cfg is not None and cfg.validator_dispatcher_effort:
            dispatcher_cmd += ["--effort", cfg.validator_dispatcher_effort]
        proc = _run_reaped(dispatcher_cmd,
                           cwd=str(cwd), env=env, capture_output=True,
                           timeout=_CLAUDE_VALIDATOR_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        err(f"validator headless run for {name} timed out after "
            f"{_CLAUDE_VALIDATOR_TIMEOUT_S}s; not trusting any artefact (fail-safe)")
        return None
    except Exception as e:
        err(f"validator headless run failed for {name}: {e}")
        return None
    # Fail-safe: a non-zero exit means we do not trust any artefact this run
    # might have left (errs toward over-block per the fail-safe posture).
    if proc.returncode != 0:
        err(f"validator headless run for {name} exited {proc.returncode}; "
            "not trusting any artefact (fail-safe)")
        return None
    if not vj.exists():
        return None
    try:
        return json.loads(vj.read_text(encoding="utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# produce — the reviewer-set orchestration (D9/D10/D11, ADR-0010/0011/0012).
# --------------------------------------------------------------------------
SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _namespace_findings(reviewer: str, findings: list[dict]) -> list[dict]:
    """Namespace each finding id `<reviewer>:<id>` so ADR-0008 id-pairing
    survives multiple producers. Mirrors aggregate.synthesize_id for id-less
    findings (the schema has no id field)."""
    out = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            continue  # skip a non-dict element defensively (M2 belt-and-suspenders)
        g = dict(f)
        raw = f.get("id") if isinstance(f.get("id"), str) and f.get("id") else f"finding-{i}"
        g["id"] = f"{reviewer}:{raw}"
        out.append(g)
    return out


def _concordance(all_findings: list[dict]) -> dict:
    """Best-effort cross-reviewer match for RANKING ONLY (ADR-0010/0011): count
    how many distinct reviewers surfaced a finding at the same (file,
    line_start). Never gate-critical — a matching error only degrades ordering.
    Returns {namespaced_id: concordance_count}."""
    buckets: dict[tuple, set] = {}
    for f in all_findings:
        key = (f.get("file"), f.get("line_start"))
        buckets.setdefault(key, set()).add(f["id"].split(":", 1)[0])
    return {f["id"]: len(buckets[(f.get("file"), f.get("line_start"))]) for f in all_findings}


# --------------------------------------------------------------------------
# Per-review findings archive (#40) — a produce-time, gitignored, advisory
# findings log (Layer-1). The citation snapshot/parse below is the ONE owner of
# the `[SEV] verdict id=<fid> file:line — citation` grammar; the Layer-2
# measurement wrapper (merge_gate_measure.py) imports these rather than
# duplicating them (and so the logic auto-mirrors to keel with this module). The
# archive itself is best-effort + behaviour-neutral: a failure never changes the
# gate verdict/exit, and it is written at PRODUCE time only — verify writes
# nothing (D1).
# --------------------------------------------------------------------------

# The validators.json line grammar (design §8 citation-preservation). Each string
# in validators[*].lines[] is:  [SEV] verdict id=<fid> <file>:<line> — <citation>
# (the separator before the citation is space, U+2014 EM DASH, space). The
# aggregate[] array has no citation, so the text is parsed out of lines[].
_CITATION_SEP = " — "  # space, EM DASH, space


def _parse_citation_line(line: str) -> tuple[str | None, str | None]:
    """Pull (fid, citation) out of one validators.json line. Returns (None, None)
    if the line lacks an `id=` token or the em-dash separator. Em-dash-in-citation
    safe — anchors on the FIRST separator after `id=`."""
    if not isinstance(line, str):
        return None, None
    marker = line.find("id=")
    if marker < 0:
        return None, None
    sep = line.find(_CITATION_SEP, marker)
    if sep < 0:
        return None, None
    fid = line[marker + 3:].split(None, 1)[0]
    if not fid:
        return None, None
    citation = line[sep + len(_CITATION_SEP):].strip()
    return fid, citation


def read_citations(tdir: Path) -> dict:
    """Snapshot {fid: citation} from the per-reviewer validators.json under tdir.
    Iterate every reviewer subdir, parse each validators[*].lines[] string: take
    the fid after `id=` and the text after the first em-dash separator. Lines that
    do not match are skipped. Best-effort: a missing dir, missing file, or bad json
    contributes nothing and never raises — return what was collected. fids are
    reviewer-namespaced, so there is no cross-subdir collision."""
    out: dict = {}
    try:
        subdirs = sorted(p for p in tdir.iterdir() if p.is_dir())
    except Exception:
        return out
    for sub in subdirs:
        try:
            data = json.loads((sub / "validators.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        for v in data.get("validators", []):
            for line in v.get("lines", []):
                fid, citation = _parse_citation_line(line)
                if fid is not None:
                    out[fid] = citation
    return out


def locate_citations(root: Path, base: str | None, diff_hash: str | None) -> dict:
    """Recompute the tuple dir from (base, diff_hash) and snapshot its validator
    citations. Best-effort + behaviour-neutral: any failure (falsy base/diff_hash,
    missing artefacts) degrades to {}."""
    if not base or not diff_hash:
        return {}
    try:
        cfg = load_config(root)
        tdir = tuple_dir(root / cfg.artifact_root, base, diff_hash)
        return read_citations(tdir)
    except Exception:
        return {}


def findings_log_path(artifact_root: Path) -> Path:
    """The per-review findings archive lives beside the tuple dirs and the
    producer lock in artifact_root — a stable sibling never clobbered by a tuple
    re-produce, and covered by whatever .gitignore / ignore_globs already cover
    artifact_root (the installer default `.merge-gate/**`)."""
    return artifact_root / "findings-log.md"


def _archive_marker(base_sha: str, diff_hash: str) -> str:
    """The idempotency key line — artefact identity (base, diff_hash). One entry
    per reviewed tuple; a re-produce of the same tuple is skipped-if-present."""
    return f"<!-- mg-archive base={base_sha} diff={diff_hash} -->"


def _render_archive_entry(summary: dict, citations: dict) -> str:
    """Render ONE light per-review entry: a key marker, a header
    (iso · commit · verdict · block_count), and one line per finding (reviewers ·
    severity · validator verdict · file:line · block) with its validator citation
    snapshot when present. Deliberately omits the Layer-2 measurement columns
    (human:* / conc / conf / rconf / organic-N) — this is the advisory archive,
    not the promotion ledger."""
    base_sha = str(summary.get("base_sha", "?"))
    diff_hash = str(summary.get("diff_hash", "?"))
    head = str(summary.get("head_sha") or "?")[:10]
    when = summary.get("produced_at_iso") or "?"
    verdict = summary.get("verdict", "?")
    block_count = summary.get("block_count", 0)
    lines = [
        _archive_marker(base_sha, diff_hash),
        f"## {when} · commit {head} · {verdict} · block_count={block_count}",
    ]
    findings = summary.get("findings", []) or []
    if not findings:
        lines.append("- (no findings)")
    for f in findings:
        revs = "+".join(f.get("producing_reviewers") or []) or "?"
        loc = f"{f.get('file')}:{f.get('line_start')}"
        flag = " ✓block" if f.get("block") else ""
        lines.append(f"- [{revs}] {f.get('severity')} {f.get('validator_verdict')} "
                     f"{loc}{flag}")
        citation = citations.get(f.get("id"))
        if citation:
            lines.append(f"  ↳ {citation}")
    return "\n".join(lines) + "\n\n"


def append_findings_archive(artifact_root: Path, tdir: Path, summary: dict) -> None:
    """Append a per-review entry to the findings archive. Best-effort and
    behaviour-neutral: any failure is swallowed so the gate verdict/exit is never
    affected (the archive is a side-record, not the gate). Idempotent on
    (base_sha, diff_hash): skip-if-present, keep the first entry — coalesce
    revisits, cache hits, and diff-preserving amends all converge on the same
    tuple and must not duplicate. Citations are read from the tuple's
    validators.json ON DISK, so it works on the cache-hit path too (where
    `_produce_one` has no in-memory per_reviewer)."""
    try:
        base_sha = str(summary.get("base_sha", ""))
        diff_hash = str(summary.get("diff_hash", ""))
        log = findings_log_path(artifact_root)
        marker = _archive_marker(base_sha, diff_hash)
        if log.exists() and marker in log.read_text(encoding="utf-8"):
            return  # dedup — this (base, diff_hash) is already archived
        citations = read_citations(tdir)
        entry = _render_archive_entry(summary, citations)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(entry)
    except Exception:
        pass  # behaviour-neutral: the archive never breaks the gate


def produce(cwd: Path, cfg: Config, base_sha: str, cd: dict, *,
            reviewer_runner=default_reviewer_runner,
            validator_runner=default_validator_runner,
            user_focus: str = "", intent_file: Path | None = None,
            force: bool = False) -> dict:
    """Run the configured reviewer set and write the tuple-keyed artefact.
    Returns the summary dict. Runners are injectable for the seam test."""
    artifact_root = cwd / cfg.artifact_root
    scope_hash = review_scope_hash(cfg)
    tdir = tuple_dir(artifact_root, base_sha, cd["diff_hash"])

    if not force:
        # Mirror cmd_verify: under tool-strict, a tool/version drift that verify
        # marks stale must also let produce refresh (Finding 7) — otherwise the
        # system wedges (only `force` could break out).
        current_tools = None
        cached = load_summary(tdir)
        if cfg.freshness_policy == "tool-strict":
            current_tools = {
                "codex_version": _codex_version(cfg),
                # #32 finding-F1: compute the CURRENT configured model (matching
                # what build_summary stores), never re-read the cached summary's
                # own field (that compared cached-vs-cached → never drifted).
                # claude_model is deliberately NOT a freshness key: the ACTUAL
                # model varies run-to-run (claude picks it) so it would wedge, and
                # a CONFIGURED reviewer --model change already busts
                # review_scope_hash (reviewer_args) before tool-strict is reached.
                "codex_model": _configured_reviewer_model(cfg, "codex"),
                "claude_version": _claude_version(),
                "validator_agent_version": VALIDATOR_CONTRACT_VERSION,
            }
        if freshness_state(cached, scope_hash, cfg.freshness_policy, current_tools) == "fresh":
            return cached  # cache hit — nothing to do

    # The recursion guard (MERGE_GATE_PRODUCER_RUNNING=1) is set in the CHILD env
    # of every headless `claude -p` produce spawns — the validator
    # (default_validator_runner) AND the Claude reviewer (_run_claude_reviewer) —
    # so each nested session's Stop hook no-ops and never schedules a nested
    # produce (#24/#26/#28/#29 fail-class). produce never mutates its own env.
    per_reviewer: list[dict] = []
    all_namespaced: list[dict] = []
    t_total = time.time()

    for reviewer in cfg.reviewers:
        sub_dir = tdir / reviewer
        sub_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        jsonl, exit_code = reviewer_runner(reviewer, cfg, cd, sub_dir, cwd, user_focus)
        # Per-reviewer normalize dispatch (#32): the Claude adapter brings
        # Claude's own envelope to the same .result.findings[] shape; codex and
        # custom-cmd reviewers route through the Codex JSONL adapter.
        normalized = normalize(reviewer, jsonl, exit_code)
        findings = _namespace_findings(reviewer, normalized["result"].get("findings", []))
        normalized["result"]["findings"] = findings
        all_namespaced.extend(findings)
        (sub_dir / "findings.json").write_text(
            json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
        reviewer_elapsed = time.time() - t0
        t_v = time.time()
        validators = validator_runner(reviewer, sub_dir / "findings.json", sub_dir,
                                      cwd, intent_file, cfg)
        validator_elapsed = time.time() - t_v
        per_reviewer.append({
            "reviewer": reviewer,
            "codex_status": normalized["codex"]["status"],
            "model": _reviewer_model(reviewer, jsonl),  # AC#8 (#31 input)
            "findings": findings,
            "validators": validators,
            "reviewer_seconds": round(reviewer_elapsed, 3),
            "validator_seconds": round(validator_elapsed, 3),
        })

    summary = build_summary(cfg, base_sha, cd, scope_hash, per_reviewer, all_namespaced)
    summary["total_seconds"] = round(time.time() - t_total, 3)
    write_summary_atomic(tdir, summary)
    return summary


def build_summary(cfg: Config, base_sha: str, cd: dict, scope_hash: str,
                  per_reviewer: list[dict], all_namespaced: list[dict]) -> dict:
    """Assemble summary.json: hard-gating fields, recorded-only fields, and the
    per-finding evaluation inputs #31 consumes (ADR-0011). Block = union over
    validator-upheld/unsure critical/high from ANY reviewer; the confidence
    score is ranking-only and never gates."""
    concordance = _concordance(all_namespaced)
    finding_by_id = {f["id"]: f for f in all_namespaced}
    blocking = {s.lower() for s in cfg.blocking_severities}
    # Per-reviewer model (AC#8): the actual model from the reviewer's output
    # (Claude envelope) when available, else the configured `--model` arg.
    reviewer_models = {
        pr["reviewer"]: (pr.get("model")
                         or _configured_reviewer_model(cfg, pr["reviewer"]))
        for pr in per_reviewer
    }

    # Map each reviewer's validator aggregate back to its findings.
    findings_eval: list[dict] = []
    block_count = 0
    for pr in per_reviewer:
        agg = (pr.get("validators") or {}).get("aggregate", []) or []
        agg_by_id = {a["finding_id"]: a for a in agg if "finding_id" in a}
        for f in pr["findings"]:
            fid = f["id"]
            a = agg_by_id.get(fid, {})
            # F2: a missing aggregate entry (whole validator absent) must be
            # treated as fail-safe unsure, never silently un-blocked.
            verdict = a.get("verdict", "unsure")
            # Defense-in-depth (m1 + #32 finding-1): normalize severity with
            # strip().lower() and treat ANY value outside the known SEVERITIES
            # set — including a MISSING/empty one — as blocking. The Codex path is
            # enum-constrained by --output-schema, but a soft-schema reviewer
            # (Claude) or a custom reviewer's stdout is not, so an off-enum string
            # ("CRITICAL", "blocker") AND a finding that OMITS severity entirely
            # must OVER-block (fail-safe), never silently un-block a real critical
            # by defaulting to "low" (the #18/#24 fail-open class). "unknown" is a
            # deliberate non-SEVERITIES sentinel recorded for #31.
            raw_sev = a.get("severity") or f.get("severity")
            severity = (str(raw_sev).strip().lower()
                        if raw_sev not in (None, "") else "unknown")
            unknown_sev = severity not in SEVERITIES
            # F6 + F2: honor cfg.blocking_severities; unsure crit/high still
            # blocks, but a genuine validator dismiss un-blocks even on critical.
            block = (unknown_sev or severity in blocking) and (verdict in ("uphold", "unsure"))
            if block:
                block_count += 1
            conc = concordance.get(fid, 1)
            weight = SEVERITY_WEIGHT.get(severity, 1)
            findings_eval.append({
                "id": fid,
                "producing_reviewers": [fid.split(":", 1)[0]],
                "file": f.get("file"),
                "line_start": f.get("line_start"),
                "severity": severity,
                "validator_verdict": verdict,
                "block": block,
                # Explicit concordance count (#32 open call): how many distinct
                # reviewers surfaced this (file, line_start). Stored directly so
                # #31's composition math reads it instead of recovering it as
                # confidence_score ÷ severity_weight. Ranking/measurement ONLY.
                "concordance_count": conc,
                # ADR-0011 derived confidence — concordance × severity × weight.
                # Ranking/measurement ONLY; never changes block/no-block.
                "confidence_score": conc * weight,
                # The reviewer's OWN per-finding self-report (schema 0..1),
                # recorded distinctly (ADR-0011).
                "reviewer_confidence": f.get("confidence"),
            })

    # F3: a reviewer that did not return ok was never actually reviewed; that
    # must force a non-pass verdict, not a 0-findings pass.
    reviewer_failures = [pr["reviewer"] for pr in per_reviewer
                         if pr.get("codex_status") != "ok"]
    if reviewer_failures:
        verdict = "error"
    elif block_count > 0:
        verdict = "block"
    else:
        verdict = "pass"
    produced_ts = int(time.time())
    return {
        # hard-gating fields (mismatch ⇒ re-review)
        "schema_version": SCHEMA_VERSION,
        "base_sha": base_sha,
        "diff_hash": cd["diff_hash"],
        "review_scope_hash": scope_hash,
        # recorded-only fields (ignored for freshness; audit/measurement)
        "verdict": verdict,
        "block_count": block_count,
        "reviewer_failures": reviewer_failures,
        "produced_at": produced_ts,
        # #31 readability: human-legible UTC mirror of produced_at, so the
        # gitignored cache artefact is browsable without epoch conversion.
        "produced_at_iso": datetime.datetime.fromtimestamp(
            produced_ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head_sha": None,  # filled by caller (cmd_produce) which knows cwd
        "base_ref": cfg.base_ref,
        "enforcement_policy_at_produce": cfg.enforcement_policy,
        "producer_version": CANONICAL_DIFF_ALGO_VERSION,
        "codex_version": _codex_version(cfg),
        # AC#8: per-reviewer model provenance (#31 input). Claude's actual model
        # comes from its envelope (per_reviewer["model"]); Codex's `--version`
        # does not expose the model, so its configured `--model` arg is the
        # operator-visible record (else None). codex_model stays populated for
        # the tool-strict freshness check (freshness_state) that consumes it.
        "codex_model": reviewer_models.get("codex"),
        "claude_model": reviewer_models.get("claude"),
        "reviewer_models": reviewer_models,
        "claude_version": _claude_version(),
        "validator_agent_version": VALIDATOR_CONTRACT_VERSION,
        # #47: validator model provenance (configured values; None = tool
        # default). Recorded-only, mirrors the reviewer_models record above.
        "validator_model": cfg.validator_model,
        "validator_dispatcher_model": cfg.validator_dispatcher_model,
        "reviewers": cfg.reviewers,
        "changed_files": cd["changed_files"],
        "skipped_untracked": cd["skipped_untracked"],
        "per_reviewer_timings": [
            {"reviewer": pr["reviewer"], "codex_status": pr["codex_status"],
             "model": pr.get("model") or reviewer_models.get(pr["reviewer"]),
             "reviewer_seconds": pr["reviewer_seconds"],
             "validator_seconds": pr["validator_seconds"]}
            for pr in per_reviewer
        ],
        "findings": findings_eval,
    }


def _codex_version(cfg: Config) -> str | None:
    try:
        p = subprocess.run([cfg.reviewer_bin("codex"), "--version"],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip() or None
    except Exception:
        return None


def _claude_version() -> str | None:
    try:
        p = subprocess.run(["claude", "--version"], capture_output=True,
                           text=True, timeout=10)
        return p.stdout.strip() or None
    except Exception:
        return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _published_range_messages(cwd: Path, base_sha: str, tip_sha: str) -> str:
    """Commit messages of the published range — durable validator context (D11)."""
    rc, out = git(cwd, ["log", "--format=%s%n%b", f"{base_sha}..{tip_sha}"])
    return out.strip() if rc == 0 else ""


def _is_ancestor(root: Path, anc: str, desc: str) -> bool:
    """True iff `anc` is an ancestor of (or equal to) `desc`
    (`git merge-base --is-ancestor`, which returns 0 for equal shas too)."""
    rc, _ = git(root, ["merge-base", "--is-ancestor", anc, desc])
    return rc == 0


def _pending_artefact(root: Path, cfg: Config, tip_sha: str,
                      remote_base: str | None = None):
    """Re-derive the artefact the pending producer wrote (or is writing) for the
    pushed tip, trusting the producer's recorded base (#33 G2). Returns
    (summary_or_None, base, diff_hash, pending), or None when there is no pending
    tuple matching this tip OR the producer's base is not trustworthy for this
    push. `summary` is None when the producer has not yet written the artefact
    (still spinning up, or mid-review) — the signal the wait path polls on. The
    diff is ALWAYS re-derived from the COMMITTED tip against the producer's base —
    never the working tree — so a found artefact is provably the pushed commit's
    (Finding-1).

    Base-trust guard (ADR-0014): the producer's base (A) is accepted only when it
    is an ANCESTOR of the push's resolved base (`remote_base`, B). Then the review
    range A..T is a SUPERSET of the pushed range B..T, so everything published was
    reviewed. A divergent base (multi-pusher / force-push — neither an ancestor)
    is a documented `missing` residual, NOT a pass: trusting it would report
    `fresh` for a range the artefact never covered (Finding-1 lineage)."""
    artifact_root = root / cfg.artifact_root
    pending = read_pending(artifact_root)
    if not pending or pending.get("tip_sha") != tip_sha:
        return None
    base2 = pending.get("base_sha")
    if not base2:
        # Spin-up window: the producer launched but has not computed its diff yet
        # (the hook wrote only {tip_sha, pid}). No artefact yet — poll on (G3).
        return (None, None, None, pending)
    # Reject a divergent producer base (ADR-0014 multi-pusher residual → missing).
    if remote_base and not _is_ancestor(root, base2, remote_base):
        return None
    cd2 = canonical_diff_at_commit(root, base2, tip_sha, cfg.review_globs, cfg.ignore_globs)
    if cd2.get("diff_error") or not cd2["changed_files"]:
        return (None, base2, None, pending)
    summary2 = load_summary(tuple_dir(artifact_root, base2, cd2["diff_hash"]))
    return (summary2, base2, cd2["diff_hash"], pending)


# Bounded verify-wait budget (#33 4c). Sized off the OBSERVED produce-latency
# p95, not a guessed 8 min: a real produce hit 543s (≈9 min), so the default
# leaves headroom above that. Read from the environment per-call (not at import)
# so the operator/tests can tune it. The wait closes the commit→produce→push race
# WITHOUT verify ever running Codex/Claude or writing an artefact (#30 D1): it
# only polls an INDEPENDENTLY-launched producer's tuple. Escape: git push --no-verify.
def _wait_param(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except (TypeError, ValueError):
        return float(default)


def _await_pending_artefact(root: Path, cfg: Config, scope: str, tip_sha: str,
                            current_tools: dict | None, remote_base: str | None = None):
    """G2 + G3 — resolve the pushed tip's review via the matched pending tuple,
    waiting for an in-flight producer when necessary. Returns
    {summary, base, diff_hash, state, waited, did_wait} for any TERMINAL artefact —
    `state` is its freshness ('fresh' → verify passes; 'failing'/other non-fresh →
    verify reports that state's block against the producer's base, #39) — else None
    when the artefact is GENUINELY absent (no matching tuple, divergent producer
    base, dead/orphaned producer, or wait budget exhausted → verify reports
    `missing`). A non-matching/absent tuple or a dead producer returns None
    IMMEDIATELY (no wait); a terminal artefact also returns IMMEDIATELY (it never
    waits on a failing review to "become" fresh). While waiting it runs NO Codex/Claude and
    writes NO artefact — it only re-reads the tuple and the producer's
    summary.json (D1 asymmetric-privilege preserved); the diff is always
    re-derived from the COMMITTED tip (Finding-1)."""
    budget = _wait_param("MERGE_GATE_VERIFY_WAIT_SECONDS", 900)
    poll = _wait_param("MERGE_GATE_VERIFY_WAIT_POLL_SECONDS", 2)
    hb_every = _wait_param("MERGE_GATE_VERIFY_WAIT_HEARTBEAT_SECONDS", 15)
    start = time.monotonic()
    last_hb = 0.0
    did_wait = False
    while True:
        res = _pending_artefact(root, cfg, tip_sha, remote_base)
        if res is None:
            return None  # no pending tuple for this tip (or divergent base) → missing
        summary2, base2, dh2, pending = res
        elapsed = time.monotonic() - start
        if summary2 is not None:
            # The producer has FINISHED this tip (terminal) — NEVER wait on it (a
            # failing/scope-mismatch review will not "become" fresh, so we return
            # promptly). Hand the artefact's ACTUAL freshness state + summary back
            # so verify reports the in-flight produce's real outcome against the
            # producer's base: a `fresh` artefact passes, a `failing` one is
            # reported as `review BLOCKED N finding(s)` — not the misleading
            # remote-base `missing` (#39). A genuinely-absent artefact (summary2 is
            # None below: spin-up/dead/timeout) still falls through to `missing`.
            st = freshness_state(summary2, scope, cfg.freshness_policy, current_tools)
            return {"summary": summary2, "base": base2, "diff_hash": dh2, "state": st,
                    "waited": int(round(elapsed)), "did_wait": did_wait}
        # Artefact not yet written — wait only while THIS producer is alive and
        # budget remains.
        if not pid_alive(pending.get("pid")):
            return None  # dead/orphaned producer + no artefact → missing
        if elapsed >= budget:
            err(f"in-flight produce did not finish within {int(budget)}s; "
                "reporting missing (escape: git push --no-verify)")
            return None
        if elapsed - last_hb >= hb_every:
            # Heartbeat to stderr so a multi-minute wait does not look hung; the
            # measurement wrapper streams these live (G4).
            sys.stderr.write(f"merge-gate: waiting for in-flight produce… "
                             f"{int(elapsed)}s elapsed (pid {pending.get('pid')})\n")
            sys.stderr.flush()
            last_hb = elapsed
        did_wait = True
        time.sleep(poll)


def cmd_verify(args) -> int:
    cwd = Path(args.cwd or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        err("not a git repository")
        return 1 if args.enforcement == "client-side-blocking" else 0
    cfg = load_config(root)
    enforcement = args.enforcement or cfg.enforcement_policy
    base = resolve_base_sha(root, args.base_ref or cfg.base_ref, args.base_sha)
    if base is None:
        msg = "could not resolve base ref (missing upstream/default); run `produce` or pass --base-sha"
        err(msg)
        if enforcement == "client-side-blocking":
            print(f"merge-gate BLOCK: {msg}")
            return 1
        print(f"merge-gate advisory: {msg} (not blocking)")
        return 0
    # Verify the PUSHED commit (Finding 1), not the working tree — pre-push
    # passes --tip-sha <local_sha>. Manual verify with no tip keeps the
    # working-tree behavior so a dev can check current work.
    if args.tip_sha:
        cd = canonical_diff_at_commit(root, base, args.tip_sha, cfg.review_globs, cfg.ignore_globs)
    else:
        cd = canonical_diff(root, base, cfg.review_globs, cfg.ignore_globs)
    diff_error = cd.get("diff_error")
    if not diff_error and not cd["changed_files"]:
        print("merge-gate: no in-scope changes to gate — PASS")
        return 0
    scope = review_scope_hash(cfg)
    tdir = tuple_dir(root / cfg.artifact_root, base, cd["diff_hash"])
    summary = load_summary(tdir)
    # Only pay for `--version` probes under tool-strict; content stays fast.
    current_tools = None
    if cfg.freshness_policy == "tool-strict":
        current_tools = {
            "codex_version": _codex_version(cfg),
            # #32 finding-F1: current configured model (see produce()); claude_model
            # is intentionally not a freshness key (would wedge on the actual model).
            "codex_model": _configured_reviewer_model(cfg, "codex"),
            "claude_version": _claude_version(),
            "validator_agent_version": VALIDATOR_CONTRACT_VERSION,
        }
    # An unreadable diff (base/tip object missing, git error) must block under
    # client-side-blocking, never silently pass (Finding 4). Routed through the
    # same tail that honors the bypass trailer.
    if diff_error:
        state = "unreviewable"
    else:
        state = freshness_state(summary, scope, cfg.freshness_policy, current_tools)
    tip = args.tip_sha or rev_parse(root, "HEAD")

    if state == "fresh":
        print(f"merge-gate: fresh passing review for {base[:10]}..{cd['diff_hash'][:10]} — PASS")
        return 0

    # #33 G2/G3 — before declaring `missing`, consult the pending-produce tuple.
    # The auto-producer hashes its LOCAL base while verify resolved the REMOTE
    # base; if a stale local origin diverged them the artefact sits at the
    # producer's base (G2), and a push immediately after commit may still be
    # in flight (G3). Only on `missing` (no artefact at the remote base) and only
    # for a real pushed tip — never overrides a `failing`/`scope-mismatch` artefact
    # that DOES exist at the remote base.
    if state == "missing" and args.tip_sha:
        hit = _await_pending_artefact(root, cfg, scope, args.tip_sha, current_tools,
                                      remote_base=base)
        if hit is not None:
            if hit["did_wait"]:
                # G1: a stable stdout token the measurement wrapper parses into
                # verify_wait_seconds (never recomputed wrapper-side). Emitted for
                # ANY waited-on outcome (pass OR fail) — it records the wall-clock
                # the push waited on the in-flight produce, independent of verdict.
                print(f"merge-gate: waited {hit['waited']}s for in-flight produce")
            if hit["state"] == "fresh":
                print(f"merge-gate: fresh passing review for "
                      f"{hit['base'][:10]}..{hit['diff_hash'][:10]} — PASS")
                return 0
            # #39: the in-flight produce finished NON-fresh (e.g. failing findings)
            # at the producer's base. Adopt its state + summary so the tail below
            # reports the real outcome (`review BLOCKED N finding(s)`) instead of
            # the remote-base `missing`. The block DECISION is unchanged — both
            # paths exit non-zero under enforcement; only the printed state/detail
            # changes (a reporting/label fix, not an Axis-A reset).
            state = hit["state"]
            summary = hit["summary"]

    if state == "failing" and summary and summary.get("reviewer_failures"):
        failing_detail = ("reviewer(s) failed: "
                          + ", ".join(summary["reviewer_failures"])
                          + " — review incomplete")
    else:
        failing_detail = f"review BLOCKED {summary.get('block_count') if summary else '?'} finding(s)"
    detail = {
        "missing": "no review artefact for this base+diff",
        "scope-mismatch": "review scope changed since the artefact was produced",
        "schema-incompatible": "artefact schema is incompatible",
        "tool-drift": "reviewer/validator tool version changed (freshness_policy=tool-strict)",
        "unreviewable": "could not compute the review diff for the pushed range "
                        "(base/tip object missing or git error)",
        "failing": failing_detail,
    }.get(state, state)

    if enforcement != "client-side-blocking":
        print(f"merge-gate advisory: {detail} — not blocking (advisory profile)")
        return 0

    # client-side-blocking — honor an audited tip-commit bypass (D6).
    if tip:
        reason = tip_bypass_reason(root, tip, cfg.bypass_trailer)
        if reason:
            print(f"merge-gate BYPASSED: {reason}")
            return 0
    print(f"merge-gate BLOCK: {detail}.\n"
          f"  Fix: make your working tree match the commit you're pushing\n"
          f"  (commit or stash in-scope changes — `produce` reviews the working\n"
          f"  tree, so a dirty tree seeds a different review than the pushed\n"
          f"  commit), then run `merge-gate-local produce` (or `force`). Or add\n"
          f"  a `{cfg.bypass_trailer}: <reason>` trailer to the tip commit, or\n"
          f"  push with `git push --no-verify` (unaudited).")
    return 1


# --------------------------------------------------------------------------
# Consumer-side findings read (#49, ADR-0027). The implementing session's read
# step for the advisory reproduce-or-refute loop: surface each finding's REVIEWER
# CONTENT joined with the gate's verdict + validator citation, waiting for an
# in-flight produce. WRITES NOTHING (D1, instrument-around ADR-0009) — it only
# re-reads the tuple artefacts the gate already produced. The validator verdict
# travels as a HINT, never a filter (#31 measured it 100% over-blocking).
# --------------------------------------------------------------------------
def _reviewer_finding_content(tdir: Path) -> dict:
    """Snapshot {namespaced_fid: {title, body, recommendation, confidence,
    line_end}} from each reviewer's findings.json under tdir — the CONTENT a
    human/agent judges, which summary.json (the verdict spine) and findings-log.md
    (a thin index) both omit. Best-effort + behaviour-neutral: a missing dir,
    missing file, or bad json contributes nothing and never raises."""
    out: dict = {}
    try:
        subdirs = sorted(p for p in tdir.iterdir() if p.is_dir())
    except Exception:
        return out
    for sub in subdirs:
        try:
            doc = json.loads((sub / "findings.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        for f in ((doc.get("result") or {}).get("findings") or []):
            fid = f.get("id")
            if isinstance(fid, str):
                out[fid] = {"title": f.get("title"), "body": f.get("body"),
                            "recommendation": f.get("recommendation"),
                            "confidence": f.get("confidence"),
                            "line_end": f.get("line_end")}
    return out


def _join_findings(summary: dict, tdir: Path) -> list[dict]:
    """Join summary.json's per-finding eval (the verdict spine: severity,
    validator_verdict, block, location, producing_reviewers) with each reviewer's
    findings.json CONTENT (title/body/recommendation/confidence) and its validator
    citation, keyed on the namespaced finding id. A finding whose content is
    missing still appears (content fields None) — never silently dropped."""
    content = _reviewer_finding_content(tdir)
    citations = read_citations(tdir)
    joined = []
    for fe in (summary.get("findings") or []):
        fid = fe.get("id")
        c = content.get(fid, {})
        joined.append({
            "id": fid,
            "reviewers": fe.get("producing_reviewers") or [],
            "file": fe.get("file"),
            "line_start": fe.get("line_start"),
            "line_end": c.get("line_end"),
            "severity": fe.get("severity"),
            "validator_verdict": fe.get("validator_verdict"),  # HINT only (#31)
            "block": fe.get("block"),
            "reviewer_confidence": fe.get("reviewer_confidence"),
            "concordance_count": fe.get("concordance_count"),
            "title": c.get("title"),
            "body": c.get("body"),
            "recommendation": c.get("recommendation"),
            "citation": citations.get(fid),
        })
    return joined


def _emit_findings(args, payload: dict) -> int:
    """Print the findings payload — JSON with --json, else a compact human form —
    and return 0 (this is a read step, never a gate)."""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"merge-gate findings — {payload['state']}"
          f" · verdict={payload['verdict']}"
          f" · block_count={payload['block_count']}")
    if payload["waited_seconds"] is not None:
        print(f"  (waited {payload['waited_seconds']}s for in-flight produce)")
    findings = payload["findings"]
    if not findings:
        print("  (no findings)")
        return 0
    print("  validator verdict is a HINT only (#31) — judge content as-is.")
    for f in findings:
        revs = "+".join(f["reviewers"]) or "?"
        flag = " ✓block" if f["block"] else ""
        print(f"\n- [{revs}] {f['severity']} · validator={f['validator_verdict']}"
              f" · {f['file']}:{f['line_start']}{flag}")
        if f.get("title"):
            print(f"  {f['title']}")
        if f.get("body"):
            print(f"  {f['body']}")
        if f.get("recommendation"):
            print(f"  ↳ recommendation: {f['recommendation']}")
        if f.get("citation"):
            print(f"  ↳ validator citation: {f['citation']}")
    return 0


def cmd_findings(args) -> int:
    """Read-only (#49, ADR-0027) — surface the pushed tip's advisory findings for
    the consumer-side reproduce-or-refute loop. Resolves the artefact exactly as
    `verify` does (remote base, then the in-flight pending tuple via
    `_await_pending_artefact`, keyed on the pushed tip), then joins each finding's
    reviewer CONTENT with the gate's verdict + validator citation. WRITES NOTHING
    (D1). Always exits 0 — this is not a gate; the validator verdict is a HINT,
    never a filter (#31)."""
    cwd = Path(args.cwd or os.getcwd())
    payload = {
        "state": None, "base_sha": None, "diff_hash": None, "tip_sha": None,
        "head_sha": None, "verdict": None, "block_count": None,
        "produced_at_iso": None, "waited_seconds": None, "pending_tip": None,
        "validator_verdict_note": "hint only — measured unreliable (#31); "
                                  "never an include/exclude filter",
        "findings": [],
    }
    root = repo_root(cwd)
    if root is None:
        payload["state"] = "not-a-repo"
        return _emit_findings(args, payload)
    cfg = load_config(root)
    base = resolve_base_sha(root, args.base_ref or cfg.base_ref, args.base_sha)
    # Resolve the tip to a canonical sha (NOT the literal ref): the documented
    # invocation is `--tip-sha HEAD`, and the pending-tuple match
    # (_pending_artefact) keys on the post-commit hook's resolved sha — an
    # unresolved "HEAD" would fail that exact-match and silently skip the wait.
    tip = rev_parse(root, args.tip_sha or "HEAD")
    payload["tip_sha"] = tip
    if base is None or tip is None:
        payload["state"] = "no-base" if base is None else "no-tip"
        return _emit_findings(args, payload)
    cd = canonical_diff_at_commit(root, base, tip, cfg.review_globs, cfg.ignore_globs)
    if cd.get("diff_error"):
        payload["state"] = "unreviewable"
        return _emit_findings(args, payload)
    if not cd["changed_files"]:
        payload["state"] = "no-changes"
        return _emit_findings(args, payload)
    scope = review_scope_hash(cfg)
    current_tools = None
    if cfg.freshness_policy == "tool-strict":
        current_tools = {
            "codex_version": _codex_version(cfg),
            "codex_model": _configured_reviewer_model(cfg, "codex"),
            "claude_version": _claude_version(),
            "validator_agent_version": VALIDATOR_CONTRACT_VERSION,
        }
    r_base, r_dh = base, cd["diff_hash"]
    summary = load_summary(tuple_dir(root / cfg.artifact_root, r_base, r_dh))
    if summary is None:
        # No artefact at the remote base — consult the in-flight produce (G2/G3),
        # waiting (bounded by MERGE_GATE_VERIFY_WAIT_SECONDS) for the pushed tip.
        # Time the wait locally so a matched-but-timed-out produce can report the
        # ACTUAL elapsed wait (the helper returns a bare None for that case).
        t0 = time.monotonic()
        hit = _await_pending_artefact(root, cfg, scope, tip, current_tools,
                                      remote_base=base)
        if hit is not None:
            summary = hit["summary"]
            r_base, r_dh = hit["base"], hit["diff_hash"]
            if hit["did_wait"]:
                payload["waited_seconds"] = hit["waited"]
        else:
            # The shared _await collapses three situations to None — unmatched
            # tuple, dead producer, or an in-flight produce past the wait budget.
            # Re-probe read-only to tell them apart (#49 pass-2).
            probe = _pending_artefact(root, cfg, tip, base)
            if probe is not None:
                summary2, base2, dh2, pending = probe
                if summary2 is not None:
                    # F-A: the producer finished between the wait-timeout and this
                    # re-probe — load the just-arrived artefact, do not drop it.
                    summary, r_base, r_dh = summary2, base2, dh2
                elif pid_alive(pending.get("pid")):
                    # F-B: only a still-ALIVE producer past the budget is genuinely
                    # in flight; a dead producer falls through to `missing`.
                    payload["base_sha"], payload["diff_hash"] = base2, dh2  # F-C
                    payload["state"] = "pending-timeout"
                    payload["waited_seconds"] = int(round(time.monotonic() - t0))
                    payload["pending_tip"] = tip
                    return _emit_findings(args, payload)
    payload["base_sha"], payload["diff_hash"] = r_base, r_dh
    if summary is None:
        payload["state"] = "missing"
        return _emit_findings(args, payload)
    payload["state"] = freshness_state(summary, scope, cfg.freshness_policy, current_tools)
    payload["verdict"] = summary.get("verdict")
    payload["block_count"] = summary.get("block_count")
    payload["head_sha"] = summary.get("head_sha")
    payload["produced_at_iso"] = summary.get("produced_at_iso")
    payload["findings"] = _join_findings(
        summary, tuple_dir(root / cfg.artifact_root, r_base, r_dh))
    return _emit_findings(args, payload)


def _build_intent_file(root: Path, cfg: Config, base: str, tip: str | None,
                       args) -> Path | None:
    """Durable validator context (D11): published-range commit messages + branch
    + optional operator intent, written to artifact_root/.intent.txt. Returns the
    path, or None when there is no intent to record (the payload then has no
    durable_context key)."""
    intent_parts = []
    branch = current_branch(root)
    if branch and branch != "HEAD":
        intent_parts.append(f"Branch: {branch}")
    if tip:
        msgs = _published_range_messages(root, base, tip)
        if msgs:
            intent_parts.append("Commit messages of the published range:\n" + msgs)
    if args.intent:
        intent_parts.append("Operator intent:\n" + args.intent)
    elif args.intent_from:
        try:
            intent_parts.append("Operator intent:\n" + Path(args.intent_from).read_text(encoding="utf-8"))
        except Exception as e:
            err(f"could not read --intent-from {args.intent_from}: {e}")
    if not intent_parts:
        return None
    artifact_root = root / cfg.artifact_root
    artifact_root.mkdir(parents=True, exist_ok=True)
    intent_file = artifact_root / ".intent.txt"
    intent_file.write_text("\n\n".join(intent_parts), encoding="utf-8")
    return intent_file


def _produce_one(root: Path, cfg: Config, base: str, tip_sha: str | None, args,
                 reviewer_runner, validator_runner) -> dict | None:
    """Produce ONE tuple and record its pending hand-off. With `tip_sha` the
    review is COMMIT-PINNED (canonical_diff_at_commit — the committed tip, robust
    on a dirty tree, #33 4b); without it the working tree is reviewed (manual
    produce, unchanged). Returns the canonical-diff dict on a real produce (so the
    coalescing caller can dedup on diff_hash) or None on no-in-scope / diff-error.
    Must be called while holding the ProducerLock."""
    if tip_sha:
        cd = canonical_diff_at_commit(root, base, tip_sha, cfg.review_globs, cfg.ignore_globs)
    else:
        cd = canonical_diff(root, base, cfg.review_globs, cfg.ignore_globs)
    if cd.get("diff_error"):
        err("could not compute the review diff (base/tip object missing or git "
            "error); refusing to write a clean artefact")
        return None
    if not cd["changed_files"]:
        print("merge-gate produce: no in-scope changes — nothing to review")
        return None
    tip = rev_parse(root, tip_sha) if tip_sha else rev_parse(root, "HEAD")
    intent_file = _build_intent_file(root, cfg, base, tip, args)
    # EXPLICIT --intent / --intent-from bypasses the cache (C4) so the operator's
    # durable intent reaches the reviewer+validator; the auto-producer passes
    # neither, so auto-derived branch/commit-message intent does not bust the
    # cache (intent is kept out of review_scope_hash to protect D4).
    effective_force = bool(getattr(args, "force", False)) or bool(args.intent or args.intent_from)
    summary = produce(root, cfg, base, cd, user_focus=args.intent or "",
                      intent_file=intent_file, force=effective_force,
                      reviewer_runner=reviewer_runner, validator_runner=validator_runner)
    if tip:
        summary["head_sha"] = tip
        write_summary_atomic(tuple_dir(root / cfg.artifact_root, base, cd["diff_hash"]), summary)
    # Per-review findings archive (#40): append this review's findings + validator
    # citations to a produce-time, gitignored, advisory log. Both the auto path
    # (post-commit coalesce) and the manual path reach here; best-effort and
    # behaviour-neutral (a failure never changes the verdict/exit), produce-time
    # only (D1 — verify writes nothing).
    append_findings_archive(root / cfg.artifact_root,
                            tuple_dir(root / cfg.artifact_root, base, cd["diff_hash"]),
                            summary)
    # Pending hand-off for the pre-push verify (G2/G3): tip_sha is the
    # verify-match key, base_sha lets verify trust THIS producer's base (a stale
    # local origin would otherwise diverge it), pid keys the bounded wait off
    # this producer's liveness.
    write_pending(root / cfg.artifact_root,
                  {"base_sha": base, "diff_hash": cd["diff_hash"],
                   "tip_sha": tip, "pid": os.getpid()})
    # G5: ⓑ counts this literal line in produce.log — keep the wording stable.
    # flush=True is load-bearing under coalescing: the post-commit producer
    # redirects stdout to produce.log (a FILE → block-buffered), and a coalescing
    # run is one long-lived process emitting several verdict lines. Without the
    # flush they would all sit in the buffer until process exit, so ⓑ (read at the
    # next push) would undercount produces that completed mid-run. Flush makes each
    # completed produce visible to ⓑ immediately.
    print(f"merge-gate produce: verdict={summary['verdict']} "
          f"block_count={summary['block_count']} "
          f"({len(cfg.reviewers)} reviewer(s))", flush=True)
    return cd


def _produce_coalesce(root: Path, cfg: Config, base: str, args,
                      reviewer_runner, validator_runner) -> None:
    """Auto-produce coalescing loop (#33 B1). Holding the lock, produce the
    current committed tip, then re-evaluate HEAD; if HEAD's committed diff moved
    (a commit landed while the prior review was in flight) produce the new tip
    too. Converges when HEAD's committed diff is one already produced this run.
    `base` is resolved ONCE by the caller and held fixed — for the auto path it
    is the (lagging) local origin tip, stable until the operator pushes. The
    LockBusy producers from concurrent commits skip and trust THIS holder to
    coalesce to the latest tip."""
    produced: set[str] = set()
    while True:
        tip = rev_parse(root, "HEAD")
        if tip is None:
            break
        # Peek the committed-tip diff cheaply to dedup before the expensive
        # produce — so a converged HEAD does not re-run reviewers or double-print
        # the verdict line (which ⓑ counts, G5).
        cd_peek = canonical_diff_at_commit(root, base, tip, cfg.review_globs, cfg.ignore_globs)
        if cd_peek.get("diff_error") or not cd_peek["changed_files"]:
            if not produced:
                print("merge-gate produce: no in-scope changes — nothing to review")
            break
        if cd_peek["diff_hash"] in produced:
            break  # converged — HEAD's committed diff is already fresh
        _produce_one(root, cfg, base, tip, args, reviewer_runner, validator_runner)
        produced.add(cd_peek["diff_hash"])


def cmd_produce(args, *, reviewer_runner=default_reviewer_runner,
                validator_runner=default_validator_runner) -> int:
    cwd = Path(args.cwd or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        err("not a git repository")
        return 1
    cfg = load_config(root)
    # #47/#48: refuse a non-alias validator.model or an off-enum reasoning
    # effort BEFORE any review spend or artefact write — every produce path
    # (manual, force, coalesce, the Stop scheduler's CLI call) funnels here.
    bad_model = _invalid_validator_model(cfg) or _invalid_reasoning_effort(cfg)
    if bad_model:
        err(bad_model)
        return 1
    base = resolve_base_sha(root, args.base_ref or cfg.base_ref)
    if base is None:
        err("could not resolve base ref; pass --base-ref or set up an upstream")
        return 1
    tip_sha = getattr(args, "tip_sha", None)
    coalesce = bool(getattr(args, "coalesce", False))
    try:
        with ProducerLock(root / cfg.artifact_root):
            if coalesce:
                _produce_coalesce(root, cfg, base, args,
                                  reviewer_runner, validator_runner)
            else:
                _produce_one(root, cfg, base, tip_sha, args,
                             reviewer_runner, validator_runner)
    except LockBusy:
        err("another produce holds the lock; skipping")
        return 0
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="merge-gate-local",
                                description="local-profile merge gate (#30)")
    p.add_argument("--cwd", default=None, help="repo dir (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("verify", help="check freshness/pass of the cached review (fast, no Codex/Claude)")
    pv.add_argument("--base-sha", default=None, help="explicit base sha (pre-push passes the remote sha)")
    pv.add_argument("--tip-sha", default=None, help="tip commit of the published range (for bypass)")
    pv.add_argument("--base-ref", default=None, help="override base_ref")
    pv.add_argument("--enforcement", default=None,
                    choices=["advisory", "client-side-blocking"],
                    help="override enforcement_policy (pre-push hook may pass this)")
    pv.set_defaults(func=cmd_verify)

    pp = sub.add_parser("produce", help="run the reviewer set and write the artefact (expensive)")
    pp.add_argument("--base-ref", default=None)
    pp.add_argument("--tip-sha", default=None,
                    help="commit-pin the review to this tip (canonical_diff_at_commit) "
                         "instead of the working tree (#33 4b)")
    pp.add_argument("--coalesce", action="store_true",
                    help="auto-produce mode: pin to HEAD and re-evaluate HEAD after "
                         "each produce so a batch of commits converges to a fresh "
                         "artefact for the latest tip (#33 B1)")
    pp.add_argument("--intent", default=None, help="operator intent text (durable validator context)")
    pp.add_argument("--intent-from", default=None, help="file with operator intent")
    pp.add_argument("--force", action="store_true", help="ignore cache and re-produce")
    pp.set_defaults(func=cmd_produce)

    pfd = sub.add_parser("findings",
                         help="read-only — surface the pushed tip's advisory findings "
                              "(reviewer content + verdict + citation) for the "
                              "consumer-side reproduce-or-refute loop (#49); waits for "
                              "an in-flight produce; writes nothing")
    pfd.add_argument("--base-sha", default=None, help="explicit base sha (default: resolved base_ref)")
    pfd.add_argument("--tip-sha", default=None, help="pushed tip commit (default: HEAD)")
    pfd.add_argument("--base-ref", default=None, help="override base_ref")
    pfd.add_argument("--json", action="store_true", help="emit JSON instead of the text form")
    pfd.set_defaults(func=cmd_findings)

    pf = sub.add_parser("force", help="alias for `produce --force`")
    pf.add_argument("--base-ref", default=None)
    pf.add_argument("--tip-sha", default=None,
                    help="commit-pin the forced re-produce to this tip (#33 4b)")
    pf.add_argument("--intent", default=None)
    pf.add_argument("--intent-from", default=None)
    pf.set_defaults(func=lambda a: cmd_produce(_with_force(a)))

    args = p.parse_args(argv)
    return args.func(args)


def _with_force(args):
    args.force = True
    return args


if __name__ == "__main__":
    sys.exit(main())
