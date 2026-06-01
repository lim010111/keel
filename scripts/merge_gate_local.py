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
    JSONL is normalized to `.result.findings[]` by a port of the frozen GHA
    "Normalize Codex JSONL" step (G1), shape-equivalent to what the GHA
    template feeds `/run-codex-validators --codex-json`.
  * The validator (uphold/dismiss) runs in its OWN headless context, never the
    implementing session (#24/#26/#29 fail-open lessons); produce sets
    MERGE_GATE_PRODUCER_RUNNING=1 around the headless calls.

This module is import-safe (the hooks and tests import its helpers); the CLI
lives under ``main``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
CLAUDE_DIR = HOME / ".claude"
ASSETS_DIR = CLAUDE_DIR / "scripts" / "merge-gate-assets"
ADVERSARIAL_PROMPT_PATH = ASSETS_DIR / "adversarial-review.md"
# The schema is reused (not re-vendored) from the byte-identical template copy
# — see merge-gate-assets/PROVENANCE.md.
SCHEMA_PATH = CLAUDE_DIR / "skills" / "setup-merge-gate" / "templates" / "review-output.schema.json"

# Ported from codex-plugin-cc git.mjs formatUntrackedFile: untracked files
# larger than this are excluded from the review tree and recorded, rather than
# bloating the reviewed diff. Tracked changes are never capped.
MAX_UNTRACKED_BYTES = 24 * 1024

# Stable diff flags — ported from git.mjs. No environment-dependent behaviour
# (--no-ext-diff disables any user diff driver; --binary makes binary changes
# part of the canonical bytes; --submodule=diff makes submodule bumps visible).
DIFF_FLAGS = ["--binary", "--no-ext-diff", "--submodule=diff"]

DEFAULT_ARTIFACT_ROOT = ".codex-review/local"
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
    "ignore_globs": [".codex-review/**"],
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
    The wrapper only ever reads the LOCAL profile's keys — it must not depend
    on GHA-profile keys (frozen, ADR-0009)."""
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
    return Config(data)


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def err(msg: str) -> None:
    sys.stderr.write(f"merge-gate-local: {msg}\n")


def glob_to_regex(pattern: str) -> re.Pattern:
    """fnmatch-with-** → regex. Ported verbatim from the GHA template's
    docs-only `to_regex` (codex-review.yml) so local scope-matching and the
    frozen workflow agree on glob semantics."""
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
        "reviewer_args": {r: cfg.reviewer_args(r) for r in cfg.reviewers},
        # A different reviewer binary or custom command is a different reviewer
        # implementation and MUST invalidate the cache (one-time bust on upgrade).
        "reviewer_bin": {r: cfg.reviewer_bin(r) for r in cfg.reviewers},
        "reviewer_cmd": {r: cfg.reviewer_cmd(r) for r in cfg.reviewers},
    }
    blob = json.dumps(components, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return _hash(blob)


# --------------------------------------------------------------------------
# G1 — Normalize Codex `--json` JSONL → single-doc `.result.findings[]`.
# Port of the frozen GHA "Normalize Codex JSONL" jq step (codex-review.yml,
# ADR-0012). The output shape MUST be byte-equivalent to what the GHA template
# feeds `/run-codex-validators --codex-json`. Five branches, in the same order:
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


def _configured_reviewer_model(cfg: Config, reviewer: str) -> str | None:
    """The `--model`/`-m` value from a reviewer's configured args, else None —
    the operator-visible model record when the tool output doesn't carry one
    (e.g. Codex). Mirrors _unsafe_reviewer_arg's `key[=val]` / `key val`
    handling for the model flag only."""
    args = cfg.reviewer_args(reviewer) or []
    i, n = 0, len(args)
    while i < n:
        head, eq, inline = str(args[i]).partition("=")
        if head in ("--model", "-m"):
            if eq:
                return inline or None
            return str(args[i + 1]) if i + 1 < n else None
        i += 1
    return None


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


def default_reviewer_runner(name: str, cfg: Config, cd: dict, sub_dir: Path,
                            cwd: Path, user_focus: str) -> tuple[str, int]:
    """Invoke one reviewer; return (jsonl_stdout, exit_code). The Codex reviewer
    is `codex exec --json --output-schema <schema> --sandbox read-only` with the
    canonical diff fed INLINE via stdin (ADR-0012) — never `codex review`, never
    model self-collection of the diff."""
    prompt = render_adversarial_prompt(cd, user_focus)
    if name == "codex":
        extra = cfg.reviewer_args("codex") or []
        bad = _unsafe_reviewer_arg(extra)
        if bad:
            return (f"refusing reviewer_args {bad!r}: not provably sandbox-neutral; "
                    "the local reviewer is read-only-sandboxed by invariant and "
                    "must not be bypassed (M1)", 2)
        cmd = [cfg.reviewer_bin("codex"), "exec", "--json",
               "--output-schema", str(SCHEMA_PATH),
               "--sandbox", "read-only", "--skip-git-repo-check",
               "-C", str(cwd)]
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
        p = subprocess.run(cmd, cwd=str(cwd), input=prompt.encode("utf-8"),
                           capture_output=True)
    except Exception as e:
        return f"reviewer invocation failed: {e}", 127
    (sub_dir / "reviewer.stderr").write_bytes(p.stderr)
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
    env (ANTHROPIC_*, PATH, HOME, …) is preserved."""
    base = {k: v for k, v in os.environ.items()
            if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
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
        p = subprocess.run(cmd, cwd=str(cwd), input=prompt.encode("utf-8"),
                           env=env, capture_output=True,
                           timeout=_CLAUDE_REVIEWER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return (f"claude reviewer timed out after {_CLAUDE_REVIEWER_TIMEOUT_S}s "
                "(fail-closed)", 124)
    except Exception as e:
        return f"claude reviewer invocation failed: {e}", 127
    (sub_dir / "reviewer.stderr").write_bytes(p.stderr)
    return p.stdout.decode("utf-8", "replace"), p.returncode


def default_validator_runner(name: str, codex_json: Path, sub_dir: Path,
                             cwd: Path, intent_file: Path | None) -> dict | None:
    """Run the validator in its OWN headless context (#24/#26/#29). Returns the
    parsed validators.json (with `aggregate`) or None on failure."""
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
    slash = (f"/run-codex-validators --codex-json {codex_json} "
             f"--soft-mode false --out-dir {sub_dir}")
    if intent_file is not None:
        slash += f" --intent-from {intent_file}"
    env = {"MERGE_GATE_PRODUCER_RUNNING": "1"}
    try:
        proc = subprocess.run(["claude", "-p", slash, "--permission-mode", "bypassPermissions"],
                              cwd=str(cwd), env={**os.environ, **env}, capture_output=True)
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
        (sub_dir / "codex.json").write_text(
            json.dumps(normalized, ensure_ascii=False), encoding="utf-8")
        reviewer_elapsed = time.time() - t0
        t_v = time.time()
        validators = validator_runner(reviewer, sub_dir / "codex.json", sub_dir,
                                      cwd, intent_file)
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
        "produced_at": int(time.time()),
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


def cmd_produce(args) -> int:
    cwd = Path(args.cwd or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        err("not a git repository")
        return 1
    cfg = load_config(root)
    base = resolve_base_sha(root, args.base_ref or cfg.base_ref)
    if base is None:
        err("could not resolve base ref; pass --base-ref or set up an upstream")
        return 1
    cd = canonical_diff(root, base, cfg.review_globs, cfg.ignore_globs)
    if cd.get("diff_error"):
        err("could not compute the review diff (base/tip object missing or git "
            "error); refusing to write a clean artefact")
        return 1
    if not cd["changed_files"]:
        print("merge-gate produce: no in-scope changes — nothing to review")
        return 0

    # Durable validator context (D11): published-range commit messages +
    # branch + optional operator intent. Default empty keeps GHA byte-identical.
    intent_parts = []
    branch = current_branch(root)
    if branch and branch != "HEAD":
        intent_parts.append(f"Branch: {branch}")
    tip = rev_parse(root, "HEAD")
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
    intent_file = None
    if intent_parts:
        artifact_root = root / cfg.artifact_root
        artifact_root.mkdir(parents=True, exist_ok=True)
        intent_file = artifact_root / ".intent.txt"
        intent_file.write_text("\n\n".join(intent_parts), encoding="utf-8")

    # Explicit operator intent must reach the reviewer+validator, so an
    # EXPLICIT --intent / --intent-from bypasses the cache (C4). The Stop
    # scheduler launches produce WITHOUT these flags, so auto-derived
    # branch/commit-message intent does not bust the cache (and does not churn
    # it — intent is deliberately kept out of review_scope_hash to protect D4).
    effective_force = bool(getattr(args, "force", False)) or bool(args.intent or args.intent_from)
    try:
        with ProducerLock(root / cfg.artifact_root):
            summary = produce(root, cfg, base, cd, user_focus=args.intent or "",
                              intent_file=intent_file, force=effective_force)
            if tip:
                summary["head_sha"] = tip
                write_summary_atomic(tuple_dir(root / cfg.artifact_root, base, cd["diff_hash"]), summary)
    except LockBusy:
        err("another produce holds the lock; skipping")
        return 0
    print(f"merge-gate produce: verdict={summary['verdict']} "
          f"block_count={summary['block_count']} "
          f"({len(cfg.reviewers)} reviewer(s))")
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
    pp.add_argument("--intent", default=None, help="operator intent text (durable validator context)")
    pp.add_argument("--intent-from", default=None, help="file with operator intent")
    pp.add_argument("--force", action="store_true", help="ignore cache and re-produce")
    pp.set_defaults(func=cmd_produce)

    pf = sub.add_parser("force", help="alias for `produce --force`")
    pf.add_argument("--base-ref", default=None)
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
