#!/usr/bin/env python3
"""Tests for the merge-gate post-commit trigger (claude-harness-work #33).

Stdlib unittest only. Run: python3 hooks/test_merge_gate_post_commit.py -v

The post-commit hook is the #33 produce trigger that replaces the cwd-bound Stop
scheduler. It is repo-scoped (fires in whatever repo the commit lands in,
independent of the Claude session cwd — the venue root cause), launches a
backgrounded COMMIT-PINNED `produce --coalesce` against HEAD when the commit
touched in-scope files, records the pending tuple for the verify hand-off (G3),
and redirects the producer's stdout to produce.log so the ⓑ auto-produce count
keeps working (G5). Popen is patched so no real produce ever spawns.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
SCRIPTS = HOOKS.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HOOKS))

import merge_gate_local as mg            # noqa: E402
import merge_gate_post_commit as mpc     # noqa: E402

# The real local-profile installer + the shipped post-commit template — so the
# real-hook regression test below installs the ACTUAL shim, not a hand-rolled one.
INSTALL_DIR = HOOKS.parent / "skills" / "setup-merge-gate" / "scripts"
sys.path.insert(0, str(INSTALL_DIR))
import install_local as il               # noqa: E402

POST_COMMIT_TEMPLATE = INSTALL_DIR.parent / "templates" / "post-commit"


def init_repo(path: Path, profile="local", extra=""):
    """A git repo with origin/main LAGGING the local tip — the production shape
    that makes base..HEAD a non-empty committed range (resolve_base_sha returns
    the remote default tip on the default branch). `extra` is appended verbatim to
    harness.toml (e.g. a `[merge-gate.local.producer]` block). Returns (env, base_sha)."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def g(*a):
        return subprocess.run(["git", "-C", str(path), *a], check=True, env=env,
                              capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    if profile is not None:
        (path / "harness.toml").write_text(
            "[merge-gate]\n"
            f'profile = "{profile}"\n' + extra)
    (path / "base.txt").write_text("hello\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    base = g("rev-parse", "HEAD").stdout.strip()
    # Simulate origin/main lagging at the base commit.
    g("update-ref", "refs/remotes/origin/main", base)
    g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return env, base


def commit(path, env, rel, content, msg, add_all=False):
    (path / rel).parent.mkdir(parents=True, exist_ok=True)
    (path / rel).write_text(content)
    spec = "-A" if add_all else rel
    subprocess.run(["git", "-C", str(path), "add", spec], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", msg], check=True, env=env)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          check=True, env=env, capture_output=True, text=True).stdout.strip()


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


class TestPostCommitMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env, self.base = init_repo(self.root)
        self._state = tempfile.TemporaryDirectory()
        self._orig_state_root = mpc.STATE_ROOT
        mpc.STATE_ROOT = Path(self._state.name)
        # Patch the launcher so no real `produce` ever spawns.
        self._orig_launch = mpc.launch_produce
        self.launched = []
        mpc.launch_produce = lambda root, sdir: (self.launched.append((root, sdir))
                                                 or _FakeProc(4242))
        self._orig_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        mpc.STATE_ROOT = self._orig_state_root
        mpc.launch_produce = self._orig_launch
        self._tmp.cleanup()
        self._state.cleanup()

    def _run_main(self, env_extra=None):
        old = dict(os.environ)
        if env_extra:
            os.environ.update(env_extra)
        try:
            with self.assertRaises(SystemExit) as cm:
                mpc.main()
            return cm.exception.code
        finally:
            os.environ.clear()
            os.environ.update(old)

    def _ar(self):
        return self.root / mg.DEFAULT_CONFIG["artifact_root"]

    def test_in_scope_commit_launches_and_writes_pending(self):
        tip = commit(self.root, self.env, "a.py", "x = 1\n", "in-scope")
        code = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.launched), 1)            # backgrounded produce launched
        pending = mg.read_pending(self._ar())
        self.assertIsNotNone(pending)
        self.assertEqual(pending["tip_sha"], tip)          # pins HEAD
        self.assertEqual(pending["base_sha"], self.base)   # the (lagging) origin base
        self.assertEqual(pending["pid"], 4242)             # the detached child's pid (G3)
        cd = mg.canonical_diff_at_commit(self.root, self.base, tip,
                                         mg.DEFAULT_CONFIG["review_globs"],
                                         mg.DEFAULT_CONFIG["ignore_globs"])
        self.assertEqual(pending["diff_hash"], cd["diff_hash"])

    def test_out_of_scope_commit_produces_nothing(self):
        # A commit touching ONLY an ignored path → no launch, no pending.
        commit(self.root, self.env, ".codex-review/junk", "z\n", "ignored only")
        code = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [])
        self.assertIsNone(mg.read_pending(self._ar()))

    def test_recursion_guard_no_launch(self):
        commit(self.root, self.env, "a.py", "x = 1\n", "in-scope")
        self._run_main(env_extra={"MERGE_GATE_PRODUCER_RUNNING": "1"})
        self.assertEqual(self.launched, [])
        self.assertIsNone(mg.read_pending(self._ar()))

    def test_not_a_git_repo_is_noop(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        os.chdir(outside.name)
        code = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [])

    def test_advances_last_produce_ts(self):
        commit(self.root, self.env, "a.py", "x = 1\n", "in-scope")
        self._run_main()
        st = mpc.load_state(mpc.repo_state_dir(self.root))
        self.assertGreater(float(st.get("last_produce_ts", 0)), 0)


class TestPostCommitLaunch(unittest.TestCase):
    """The real launch_produce (Popen patched): the producer command is
    commit-pinned + coalescing, its stdout is redirected to the state dir's
    produce.log (G5), and the recursion-guard env var is stripped from the child."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._state = tempfile.TemporaryDirectory()
        self.sdir = Path(self._state.name) / "repo"

    def tearDown(self):
        self._tmp.cleanup()
        self._state.cleanup()

    def test_launch_uses_coalesce_and_redirects_to_produce_log(self):
        captured = {}

        class _P:
            def __init__(self, argv, cwd=None, env=None, stdout=None, stderr=None,
                         start_new_session=None):
                captured["argv"] = argv
                captured["cwd"] = cwd
                captured["env"] = env
                captured["stdout"] = stdout
                self.pid = 777
        orig = mpc.subprocess.Popen
        os.environ["MERGE_GATE_PRODUCER_RUNNING"] = "1"
        try:
            mpc.subprocess.Popen = _P
            proc = mpc.launch_produce(self.root, self.sdir)
        finally:
            mpc.subprocess.Popen = orig
            os.environ.pop("MERGE_GATE_PRODUCER_RUNNING", None)
        self.assertEqual(proc.pid, 777)
        argv = captured["argv"]
        self.assertIn("produce", argv)
        self.assertIn("--coalesce", argv)                    # commit-pinned coalescing
        # the child must NOT inherit the recursion guard (it IS the producer)
        self.assertNotIn("MERGE_GATE_PRODUCER_RUNNING", captured["env"])
        # stdout redirected so the producer's `verdict=` line lands in produce.log (G5)
        self.assertTrue((self.sdir / "produce.log").exists())


class TestPostCommitRealHook(unittest.TestCase):
    """Regression: drive a REAL `git commit` through the INSTALLED post-commit hook
    — the actual `.git/hooks/post-commit` shell shim → `merge_gate_post_commit.py`
    chain, fired by git with the genuine post-commit env (cwd = work-tree root,
    GIT_INDEX_FILE/GIT_PREFIX exported) — and assert the trigger's SYNCHRONOUS
    side-effects: the pending hand-off tuple is written and last_produce_ts advances.

    TestPostCommitMain above calls main() in-process with launch_produce stubbed;
    this codifies the end-to-end shim+git-invocation path the deploy verification
    only checked by hand. The backgrounded producer is neutralized by CONFIG, not
    by patching (patching can't reach a detached subprocess): `reviewers = []` makes
    produce's reviewer loop run zero times, so NO codex/claude subprocess can spawn
    — the test spends ZERO real API. (The reviewer/produce path itself is covered by
    the injected-runner produce tests in test_merge_gate_local.py.)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # `reviewers = []` → the `produce --coalesce` the hook backgrounds runs its
        # reviewer loop zero times → no real Codex/Claude is ever invoked.
        self.env, self.base = init_repo(
            Path(self._tmp.name),
            extra="[merge-gate.local.producer]\nreviewers = []\n")
        # Canonicalize the root exactly as the hook does (git rev-parse
        # --show-toplevel) so our state-dir hash + artefact paths match the
        # subprocess's byte-for-byte.
        self.root = mg.repo_root(Path(self._tmp.name))
        self._state = tempfile.TemporaryDirectory()
        self.state_root = Path(self._state.name)
        # The hook is a real subprocess; route its per-repo state writes into the
        # throwaway dir via the env git passes through to the hook (so we never
        # touch the real ~/.claude/hooks/.merge-gate-state).
        self.env["MERGE_GATE_STATE_ROOT"] = str(self.state_root)
        # Hermeticity: pin the shim to the CHECKED-OUT helper (the module this test
        # imports) rather than the shim's $HOME/.claude/hooks/... default, so the
        # test exercises the produce trigger ON THIS BRANCH — not whatever is
        # installed in the real ~/.claude. git exports self.env to the post-commit
        # hook, and the shim honours MERGE_GATE_POST_COMMIT over its default.
        self.env["MERGE_GATE_POST_COMMIT"] = str(Path(mpc.__file__).resolve())
        # Install the REAL hook via the REAL installer + shipped template.
        dest = il.install_post_commit(self.root, POST_COMMIT_TEMPLATE)
        self.assertTrue(dest.exists())
        self.assertIn(il.POST_COMMIT_MARKER, dest.read_text())  # it IS our shim

    def tearDown(self):
        self._tmp.cleanup()
        self._state.cleanup()

    def _artifact_root(self):
        return self.root / mg.DEFAULT_CONFIG["artifact_root"]

    def _state_dir(self):
        h = hashlib.sha1(str(self.root).encode()).hexdigest()[:16]
        return self.state_root / h

    def _commit_inscope(self, rel, content, msg):
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / rel).write_text(content)
        subprocess.run(["git", "-C", str(self.root), "add", rel],
                       check=True, env=self.env, capture_output=True)
        # The real post-commit hook fires SYNCHRONOUSLY inside this commit.
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", msg],
                       check=True, env=self.env, capture_output=True)
        return subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                              check=True, env=self.env, capture_output=True,
                              text=True).stdout.strip()

    def test_installed_hook_on_real_commit_writes_pending_and_advances_state(self):
        tip = self._commit_inscope("a.py", "x = 1\n", "in-scope")

        # --- synchronous side-effect 1: the pending hand-off tuple (G3) ---
        pending = mg.read_pending(self._artifact_root())
        self.assertIsNotNone(
            pending, "the installed post-commit hook wrote no .pending.json")
        self.assertEqual(pending["base_sha"], self.base)    # the lagging origin base
        self.assertEqual(pending["tip_sha"], tip)           # pins the committed HEAD
        cd = mg.canonical_diff_at_commit(self.root, self.base, tip,
                                         mg.DEFAULT_CONFIG["review_globs"],
                                         mg.DEFAULT_CONFIG["ignore_globs"])
        self.assertEqual(pending["diff_hash"], cd["diff_hash"])
        # pid: the detached child's pid recorded at hook time. The coalescing
        # producer may rewrite the SAME tuple (identical base/diff/tip for a single
        # commit) with its own pid, so assert shape — not an exact value.
        self.assertIsInstance(pending["pid"], int)
        self.assertGreater(pending["pid"], 0)

        # --- synchronous side-effect 2: last_produce_ts advanced (ⓑ timestamp) ---
        st = json.loads((self._state_dir() / "state.json").read_text())
        self.assertGreater(float(st.get("last_produce_ts", 0)), 0)


class TestResolveImportRoots(unittest.TestCase):
    """claude-harness-work#36: the helper's import roots resolve from its OWN
    checkout (so a checkout outside ~/.claude runs ITS produce deps), gated on the
    FULL import closure — BOTH merge_gate_local (SCRIPTS) and merge_gate_scheduler
    (HOOKS) present — with a $HOME/.claude fallback otherwise. A half-co-located
    layout must fall back, not pin (claude:finding-0). The INSTALLED layout
    ($HOME/.claude/hooks/<helper>) resolves byte-identical to the pre-#36 hardcoded
    `Path.home()/.claude/{scripts,hooks}`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _lay_out(hooks_dir, *, local=True, scheduler=True, prompt=True, schema=True):
        """A hooks/+scripts/ layout, optionally placing each member of the COMPLETE
        runtime set (#37): the two .py import-closure modules AND the two producer
        assets (adversarial-review prompt + review-output schema). Returns the hooks
        dir."""
        root = hooks_dir.parent
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        present = [
            (local, ("scripts", "merge_gate_local.py")),
            (scheduler, ("hooks", "merge_gate_scheduler.py")),
            (prompt, ("scripts", "merge-gate-assets", "adversarial-review.md")),
            (schema, ("skills", "setup-merge-gate", "templates", "review-output.schema.json")),
        ]
        for keep, rel in present:
            if not keep:
                continue
            p = root.joinpath(*rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# stub\n")
        return hooks_dir

    def test_prefers_checkout_when_complete_set_present(self):
        here = self._lay_out(self.tmp / "checkout" / "hooks")
        scripts, hooks = mpc.resolve_import_roots(here, Path("/nonexistent-home"))
        self.assertEqual(scripts, here.parent / "scripts")
        self.assertEqual(hooks, here)

    def test_falls_back_when_scheduler_absent(self):
        # A partial checkout (#36 claude:finding-0 / #37 unified gate): any single
        # runtime-set member absent → fall back, never pin (else the producer crashes
        # on the missing import / reads $HOME assets while running checkout code).
        here = self._lay_out(self.tmp / "checkout" / "hooks", scheduler=False)
        home = self.tmp / "home"
        scripts, hooks = mpc.resolve_import_roots(here, home)
        self.assertEqual(scripts, home / ".claude" / "scripts")
        self.assertEqual(hooks, home / ".claude" / "hooks")

    def test_falls_back_when_local_absent(self):
        here = self._lay_out(self.tmp / "checkout" / "hooks", local=False)
        home = self.tmp / "home"
        scripts, hooks = mpc.resolve_import_roots(here, home)
        self.assertEqual(scripts, home / ".claude" / "scripts")
        self.assertEqual(hooks, home / ".claude" / "hooks")

    def test_falls_back_when_prompt_asset_absent(self):
        # #37 unified gate: .py closure present but the prompt asset absent → MUST
        # fall back (pinning imports while assets fall back to $HOME is the decoupling
        # f60c84a claude:finding-0 flagged).
        here = self._lay_out(self.tmp / "checkout" / "hooks", prompt=False)
        home = self.tmp / "home"
        scripts, hooks = mpc.resolve_import_roots(here, home)
        self.assertEqual(scripts, home / ".claude" / "scripts")
        self.assertEqual(hooks, home / ".claude" / "hooks")

    def test_falls_back_when_schema_asset_absent(self):
        here = self._lay_out(self.tmp / "checkout" / "hooks", schema=False)
        home = self.tmp / "home"
        scripts, hooks = mpc.resolve_import_roots(here, home)
        self.assertEqual(scripts, home / ".claude" / "scripts")
        self.assertEqual(hooks, home / ".claude" / "hooks")

    def test_installed_helper_resolves_byte_identical_to_prefix(self):
        # The production install: $HOME/.claude/hooks/<helper> + sibling scripts/,
        # complete runtime set present → checkout branch == pre-#36/#37 paths.
        home = self.tmp / "home"
        here = self._lay_out(home / ".claude" / "hooks")
        scripts, hooks = mpc.resolve_import_roots(here, home)
        self.assertEqual(scripts, home / ".claude" / "scripts")   # == pre-#36/#37
        self.assertEqual(hooks, home / ".claude" / "hooks")       # == pre-#36/#37

    def test_unified_gate_predicates_agree(self):
        """#37 unified gate: the hook's import gate (resolve_import_roots) and
        merge_gate_local's asset gate (resolve_claude_dir) pin-or-fall-back
        IDENTICALLY for every layout, so a partial checkout never pins one layer
        while falling back the other (f60c84a claude:finding-0). The two predicates
        are DUPLICATED (the hook must decide which merge_gate_local to import before
        it can read RUNTIME_SET_RELPATHS), so this pins them equal across the complete
        set and every single-member-dropped layout."""
        members = mg.RUNTIME_SET_RELPATHS
        home = self.tmp / "home"
        for drop in (None, *range(len(members))):
            with self.subTest(drop=drop):
                root = (self.tmp / f"co_{drop}").resolve()
                (root / "hooks").mkdir(parents=True)
                (root / "scripts").mkdir(parents=True)
                for i, rel in enumerate(members):
                    if i == drop:
                        continue
                    p = root.joinpath(*rel)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("x")
                complete = drop is None
                scripts, _ = mpc.resolve_import_roots(root / "hooks", home)
                hook_pins = scripts == root / "scripts"
                cdir = mg.resolve_claude_dir(root / "scripts" / "merge_gate_local.py", home)
                asset_pins = cdir == root
                self.assertEqual(hook_pins, complete, f"hook gate disagreed (drop={drop})")
                self.assertEqual(asset_pins, complete, f"asset gate disagreed (drop={drop})")
                self.assertEqual(hook_pins, asset_pins)  # the unified-gate invariant


class TestPostCommitHermeticDeps(unittest.TestCase):
    """Regression for claude-harness-work#36 (Codex finding codex:finding-0,
    validator uphold/non-block on PR claude-config#34): the helper must resolve
    merge_gate_local / merge_gate_scheduler from ITS OWN checkout, not a hardcoded
    $HOME/.claude. Per Codex's next_steps, run the helper from a foreign checkout
    in an environment where $HOME holds NO installed copy — it must still run the
    CHECKED-OUT produce code (write the pending tuple). Pre-#36 the helper inserted
    only $HOME/.claude/scripts, so with $HOME emptied `import merge_gate_local`
    failed → mg=None → the helper exited early and wrote NO pending tuple.

    Like TestPostCommitRealHook, the backgrounded producer is neutralized by CONFIG
    (`reviewers = []`), not patching (patching can't reach a detached subprocess) —
    so it spends ZERO real API."""

    def setUp(self):
        # 1. A foreign checkout OUTSIDE ~/.claude: the real helper + its COMPLETE
        #    runtime set (#37 unified gate) — the two .py import-closure modules AND
        #    the two producer assets. With reviewers=[] the assets are never READ
        #    here, but the unified gate requires them PRESENT to pin imports to the
        #    checkout; a partial checkout falls back to $HOME together (so this test
        #    stays a faithful #36 import-closure regression after #37).
        self._co = tempfile.TemporaryDirectory()
        co = Path(self._co.name)
        (co / "hooks").mkdir()
        (co / "scripts").mkdir()
        shutil.copy(Path(mpc.__file__).resolve(),
                    co / "hooks" / "merge_gate_post_commit.py")
        shutil.copy(SCRIPTS / "merge_gate_local.py",
                    co / "scripts" / "merge_gate_local.py")
        shutil.copy(HOOKS / "merge_gate_scheduler.py",
                    co / "hooks" / "merge_gate_scheduler.py")
        prompt_dst = co / "scripts" / "merge-gate-assets" / "adversarial-review.md"
        schema_dst = co / "skills" / "setup-merge-gate" / "templates" / "review-output.schema.json"
        prompt_dst.parent.mkdir(parents=True, exist_ok=True)
        schema_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(mg.ADVERSARIAL_PROMPT_PATH, prompt_dst)
        shutil.copy(mg.SCHEMA_PATH, schema_dst)
        self.helper = co / "hooks" / "merge_gate_post_commit.py"

        # 2. A repo with origin/main lagging + reviewers=[] (zero real API).
        self._repo = tempfile.TemporaryDirectory()
        self.env, self.base = init_repo(
            Path(self._repo.name),
            extra="[merge-gate.local.producer]\nreviewers = []\n")
        self.root = mg.repo_root(Path(self._repo.name))

        # 3. $HOME emptied of any installed copy + state routed to a throwaway dir.
        self._home = tempfile.TemporaryDirectory()
        self._state = tempfile.TemporaryDirectory()
        self.helper_env = {**self.env,
                           "HOME": self._home.name,
                           "MERGE_GATE_STATE_ROOT": self._state.name}

    def tearDown(self):
        for d in (self._co, self._repo, self._home, self._state):
            d.cleanup()

    def _artifact_root(self):
        return self.root / mg.DEFAULT_CONFIG["artifact_root"]

    def test_foreign_checkout_with_empty_home_runs_checked_out_deps(self):
        # An in-scope committed range → the helper should fire produce + pending.
        (self.root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(self.root), "add", "a.py"],
                       check=True, env=self.env, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "in-scope"],
                       check=True, env=self.env, capture_output=True)
        tip = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                             check=True, env=self.env, capture_output=True,
                             text=True).stdout.strip()

        # Run the FOREIGN-checkout helper directly, cwd = the repo work-tree root,
        # with $HOME holding no ~/.claude. Pre-#36: no checked-out scripts/ on
        # sys.path → mg=None → exit 0 with NO pending tuple (the gap this closes).
        r = subprocess.run([sys.executable, str(self.helper)],
                           cwd=str(self.root), env=self.helper_env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0,
                         f"helper exited {r.returncode}; stderr:\n{r.stderr}")

        pending = mg.read_pending(self._artifact_root())
        self.assertIsNotNone(
            pending,
            "the foreign-checkout helper wrote no .pending.json — it could not "
            f"load its checked-out deps with $HOME emptied. stderr:\n{r.stderr}")
        self.assertEqual(pending["base_sha"], self.base)
        self.assertEqual(pending["tip_sha"], tip)
        cd = mg.canonical_diff_at_commit(self.root, self.base, tip,
                                         mg.DEFAULT_CONFIG["review_globs"],
                                         mg.DEFAULT_CONFIG["ignore_globs"])
        self.assertEqual(pending["diff_hash"], cd["diff_hash"])


if __name__ == "__main__":
    unittest.main()
