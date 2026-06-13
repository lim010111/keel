#!/usr/bin/env python3
"""Tests for auto_fill.py — scaffold-doctor #04, the thin-delegator fill dispatcher.

auto_fill composes the existing setup skills' OWN apply paths (never re-implements
a write), inherits their consent ladder as deterministic per-action tiers, hard-
refuses the footguns the survey found (ADR-0020 §5), and separates repo scope
(default) from a one-time global-bootstrap. Engine (harness_doctor.py) stays
read-only; the writes are the setup skills'. unittest to match the sibling suites.

Built test-first (TDD): one failing test -> minimal code -> repeat (vertical
slices). House helpers (_new_repo/_write_hook/_snapshot) mirror
test_harness_doctor.py / test_record_profile.py.
"""
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                            # auto_fill, record_profile (this dir)
sys.path.insert(0, str(_HERE.parents[2] / "scripts"))    # harness_doctor (~/.claude/scripts)

import harness_doctor
import auto_fill


def _new_repo(toml: str = None) -> Path:
    d = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    if toml is not None:
        (d / "harness.toml").write_text(toml, encoding="utf-8")
    return d


def _recorded(scaffold, ci: bool = False) -> str:
    """A harness.toml carrying a recorded [harness] profile (the #03 contract)."""
    items = ", ".join(f'"{s}"' for s in scaffold)
    return f"[harness]\nscaffold = [{items}]\nci = {str(ci).lower()}\n"


# Verbatim shape of the (since torn-down, ADR-0021) legacy github-actions
# target repos (mirrors test_harness_doctor.py): GHA keys, NO profile —
# kept as a realistic unrecognized-section regression input.
LEGACY_GHA_TOML = """[merge-gate]
soft_mode_default = 'true'
codex_review_cmd = 'codex exec --json "Run an adversarial review"'
bypass_label = 'merge-gate-bypass'
"""


class TestNoRecordedProfile(unittest.TestCase):
    def test_no_profile_is_a_noop_pointing_at_harness_doctor(self):
        # AC (Inputs): with no [harness] recorded, build_plan is a no-op — it must
        # NOT fall back to filling raw diagnose gaps; it points at /harness-doctor.
        repo = _new_repo()                       # no harness.toml -> no recorded profile
        plan = auto_fill.build_plan(repo)
        self.assertFalse(plan["profile_recorded"])
        self.assertEqual(plan["records"], [])
        self.assertTrue(any("harness-doctor" in n for n in plan["notes"]),
                        f"expected a /harness-doctor pointer in notes, got {plan['notes']}")


class TestAgentsMdFill(unittest.TestCase):
    def test_greenfield_agents_md_is_auto_filled(self):
        # Tracer bullet: a recorded scaffold with agents-md on a repo that has
        # NEITHER AGENTS.md nor CLAUDE.md -> apply() DELEGATES to setup_agents_md
        # (never re-implements the write) and creates both files. State-1 is
        # pure-create = auto tier, so it applies without a confirm.
        repo = _new_repo(_recorded(["agents-md"]))
        auto_fill.apply(repo)
        self.assertTrue((repo / "AGENTS.md").is_file())
        claude = repo / "CLAUDE.md"
        self.assertTrue(claude.is_file())
        self.assertIn("@AGENTS.md", claude.read_text(encoding="utf-8"))

    def test_migrate_state2_is_confirm_tier_and_not_auto_applied(self):
        # AC (consent): an existing populated CLAUDE.md with no AGENTS.md is the
        # State-2 MIGRATE — it must be consent_tier='confirm' (explicit go-ahead),
        # and the default auto apply() must NOT touch it (never silently rewrite a
        # populated CLAUDE.md into AGENTS.md). Pins the installer-action contract:
        # State-2 carries the first-class kind 'migrate' — consent keys on the
        # kind alone; the message is human-facing and free to reword.
        repo = _new_repo(_recorded(["agents-md"]))
        original = "# Project guidance\nlots of real content\n"
        (repo / "CLAUDE.md").write_text(original, encoding="utf-8")
        plan = auto_fill.build_plan(repo)
        am = [r for r in plan["records"] if r["concern"] == "agents-md"]
        self.assertEqual([r["consent_tier"] for r in am], ["confirm"])
        self.assertEqual(am[0]["kind"], "migrate")
        auto_fill.apply(repo)                                  # default auto tier only
        self.assertFalse((repo / "AGENTS.md").exists())        # migrate held, not applied
        self.assertEqual((repo / "CLAUDE.md").read_text(encoding="utf-8"), original)

    def test_conflict_state4_is_refuse_and_not_applied(self):
        # AC (consent): both files present but CLAUDE.md lacks @AGENTS.md is the
        # State-4 CONFLICT -> consent_tier='refuse' (report-only, manual merge);
        # apply() never touches either file.
        repo = _new_repo(_recorded(["agents-md"]))
        (repo / "AGENTS.md").write_text("# agents content\n", encoding="utf-8")
        claude = "# independent claude content, no import\n"
        (repo / "CLAUDE.md").write_text(claude, encoding="utf-8")
        plan = auto_fill.build_plan(repo)
        am = [r for r in plan["records"] if r["concern"] == "agents-md"]
        self.assertEqual([r["consent_tier"] for r in am], ["refuse"])
        auto_fill.apply(repo)
        self.assertEqual((repo / "CLAUDE.md").read_text(encoding="utf-8"), claude)

    def test_already_wired_concern_yields_no_actionable_records(self):
        # AC (idempotency foundation): a concern already in place emits only 'ok'
        # actions, which are dropped -> no records to fill, no writes.
        repo = _new_repo(_recorded(["agents-md"]))
        (repo / "AGENTS.md").write_text("# a\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        plan = auto_fill.build_plan(repo)
        self.assertEqual([r for r in plan["records"] if r["concern"] == "agents-md"], [])


def _hooks_dir(repo: Path) -> Path:
    return repo / ".git" / "hooks"


def _write_hook(repo: Path, name: str, body: str) -> None:
    h = _hooks_dir(repo)
    h.mkdir(parents=True, exist_ok=True)
    (h / name).write_text(body, encoding="utf-8")


class TestMergeGateFill(unittest.TestCase):
    def test_fresh_install_writes_local_profile_and_both_hooks(self):
        # AC (merge-gate fillable): scaffold wants merge-gate, no [merge-gate]
        # section (intent=absent) -> apply() DELEGATES to install_local's repo
        # functions: harness.toml gets the LOCAL profile and the pre-push +
        # post-commit hooks are installed (fresh repo, no marker -> auto tier).
        repo = _new_repo(_recorded(["merge-gate"]))
        auto_fill.apply(repo)
        toml = (repo / "harness.toml").read_text(encoding="utf-8")
        self.assertIn('profile = "local"', toml)
        pp = _hooks_dir(repo) / "pre-push"
        self.assertTrue(pp.is_file())
        self.assertIn("MERGE_GATE_WRAPPER", pp.read_text(encoding="utf-8"))
        self.assertTrue((_hooks_dir(repo) / "post-commit").is_file())
        # the recorded [harness] profile must survive the merge-gate write
        self.assertEqual(harness_doctor.read_recorded_profile(repo)["scaffold"],
                         ["merge-gate"])

    def test_fully_wired_is_dropped_and_pre_push_not_re_rendered(self):
        # AC (footgun, enforcement axis): both hooks already carry their markers ->
        # fully in place -> dropped (no record, like 'ok'); and the wired pre-push,
        # which carries a PREPENDED block (the #41 #31-RETIRED tombstone is the live
        # instance), is NEVER re-rendered — the block survives byte-for-byte.
        repo = _new_repo(_recorded(["merge-gate"]) + '\n[merge-gate]\nprofile = "local"\n')
        sentinel = ("# --- #31 measurement wiring RETIRED (#41) ---\n"
                    "#   export MERGE_GATE_WRAPPER=.../merge_gate_measure.py\n"
                    "# --- end retired block ---\n")
        pp_body = sentinel + "#!/bin/sh\n# MERGE_GATE_WRAPPER bare gate\nexit 0\n"
        _write_hook(repo, "pre-push", pp_body)
        _write_hook(repo, "post-commit", "#!/bin/sh\n# MERGE_GATE_POST_COMMIT\n")
        plan = auto_fill.build_plan(repo)
        self.assertEqual([r for r in plan["records"] if r["concern"] == "merge-gate"], [])
        res = auto_fill.apply(repo)
        self.assertNotIn("merge-gate", res["applied"])
        self.assertEqual((_hooks_dir(repo) / "pre-push").read_text(encoding="utf-8"),
                         pp_body)                       # prepended block intact

    def test_pre_push_wired_post_commit_missing_is_report_only_not_dropped(self):
        # Post-impl F1: enforcement=='wired' keys on the pre-push marker ALONE. A
        # gate whose pre-push is wired but whose #33 post-commit producer trigger is
        # MISSING must be surfaced report-only (not silently reported fully in
        # place), and #04 must NOT auto-install the post-commit (surgical
        # post-commit-only fill stays #07) nor re-render the wired pre-push.
        repo = _new_repo(_recorded(["merge-gate"]) + '\n[merge-gate]\nprofile = "local"\n')
        pp_body = "#!/bin/sh\n# MERGE_GATE_WRAPPER\nexit 0\n"
        _write_hook(repo, "pre-push", pp_body)          # wired pre-push, NO post-commit
        plan = auto_fill.build_plan(repo)
        mg = [r for r in plan["records"] if r["concern"] == "merge-gate"]
        self.assertEqual([r["consent_tier"] for r in mg], ["refuse"])   # surfaced, not dropped
        self.assertIn("post-commit", mg[0]["message"])
        auto_fill.apply(repo)
        self.assertNotIn("post-commit", [p.name for p in (_hooks_dir(repo)).iterdir()])  # not filled (#07)
        self.assertEqual((_hooks_dir(repo) / "pre-push").read_text(encoding="utf-8"), pp_body)

    def test_post_commit_re_render_is_gated_symmetrically(self):
        # AC (footgun, both hooks): pre-push UNWIRED but post-commit already wired
        # (its marker present, with a prepended rationale) -> apply installs the
        # missing pre-push but must NOT clobber the wired post-commit in place.
        repo = _new_repo(_recorded(["merge-gate"]) + '\n[merge-gate]\nprofile = "local"\n')
        pc_body = ("# operator rationale prelude — keep me\n"
                   "#!/bin/sh\n# MERGE_GATE_POST_COMMIT\nexit 0\n")
        _write_hook(repo, "post-commit", pc_body)       # wired post-commit, no pre-push
        auto_fill.apply(repo)
        self.assertTrue((_hooks_dir(repo) / "pre-push").is_file())   # pre-push filled
        self.assertEqual((_hooks_dir(repo) / "post-commit").read_text(encoding="utf-8"),
                         pc_body)                        # post-commit untouched

    def test_foreign_pre_push_is_confirm_tier_not_auto_applied(self):
        # AC (merge-gate tier): a foreign pre-push (file present, our marker absent)
        # will be backed up -> migrate class -> consent_tier='confirm'; the default
        # auto apply() must NOT touch the foreign hook.
        repo = _new_repo(_recorded(["merge-gate"]))      # no [merge-gate] -> fresh-fillable
        husky = "#!/bin/sh\nnpx husky pre-push\nexit 0\n"
        _write_hook(repo, "pre-push", husky)
        plan = auto_fill.build_plan(repo)
        mg = [r for r in plan["records"] if r["concern"] == "merge-gate"]
        self.assertEqual([r["consent_tier"] for r in mg], ["confirm"])
        auto_fill.apply(repo)                            # auto tier only
        self.assertEqual((_hooks_dir(repo) / "pre-push").read_text(encoding="utf-8"), husky)

    def test_legacy_gha_is_report_only_no_local_pre_push(self):
        # AC (footgun): a leftover legacy-GHA-shaped [merge-gate] (unrecognized
        # since ADR-0021) is report-only — auto-fill must NEVER install a local
        # pre-push onto a section it does not understand.
        repo = _new_repo(_recorded(["merge-gate"]) + "\n" + LEGACY_GHA_TOML)
        plan = auto_fill.build_plan(repo)
        mg = [r for r in plan["records"] if r["concern"] == "merge-gate"]
        self.assertEqual([r["consent_tier"] for r in mg], ["refuse"])
        auto_fill.apply(repo)
        self.assertFalse((_hooks_dir(repo) / "pre-push").exists())

    def test_github_actions_profile_is_report_only_local_only(self):
        # AC (local-only): a leftover github-actions profile value (removed,
        # ADR-0021) is report-only — never silently force-converted to local.
        repo = _new_repo(_recorded(["merge-gate"])
                         + '\n[merge-gate]\nprofile = "github-actions"\n')
        plan = auto_fill.build_plan(repo)
        mg = [r for r in plan["records"] if r["concern"] == "merge-gate"]
        self.assertEqual([r["consent_tier"] for r in mg], ["refuse"])
        auto_fill.apply(repo)
        self.assertFalse((_hooks_dir(repo) / "pre-push").exists())

    def test_linked_worktree_merge_gate_is_fillable_not_refused(self):
        # Post-impl F4: a linked worktree's hooks resolve to the SHARED common
        # .git/hooks (outside the worktree working path) — that is the same repo,
        # not a two-scope leak. merge-gate must be fillable there, not report-only.
        main = _new_repo(_recorded(["merge-gate"]))
        subprocess.run(["git", "-C", str(main), "add", "harness.toml"], check=True)
        subprocess.run(["git", "-C", str(main), "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "-m", "init"], check=True)
        linked = Path(tempfile.mkdtemp()) / "wt"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked)],
                       check=True)
        plan = auto_fill.build_plan(linked)
        mg = [r for r in plan["records"] if r["concern"] == "merge-gate"]
        self.assertEqual([r["consent_tier"] for r in mg], ["auto"])   # fillable, not refuse

    def test_out_of_repo_hookspath_is_report_only(self):
        # AC (two-scope leak): an absolute core.hooksPath escaping the repo makes a
        # "repo-scope" hook write land outside the repo -> report-only, nothing written.
        repo = _new_repo(_recorded(["merge-gate"]))
        external = Path(tempfile.mkdtemp())
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(external)],
                       check=True)
        plan = auto_fill.build_plan(repo)
        mg = [r for r in plan["records"] if r["concern"] == "merge-gate"]
        self.assertEqual([r["consent_tier"] for r in mg], ["refuse"])
        self.assertIn("core.hooksPath", mg[0]["message"])
        auto_fill.apply(repo)
        self.assertFalse((external / "pre-push").exists())   # nothing written outside the repo


_CLAUDE = Path.home() / ".claude"


def _global_snapshot() -> dict:
    """sha256 of every SOURCE/CONFIG file under ~/.claude that a per-repo fill
    must never touch — settings.json + the scripts/skills/hooks subtrees, EXCLUDING
    __pycache__/*.pyc (importing the setup modules legitimately writes bytecode).
    Deliberately NOT all of ~/.claude (projects/ churns every session)."""
    snap = {}
    for r in (_CLAUDE / "settings.json", _CLAUDE / "scripts",
              _CLAUDE / "skills", _CLAUDE / "hooks"):
        if r.is_file():
            snap[str(r)] = hashlib.sha256(r.read_bytes()).hexdigest()
        elif r.is_dir():
            for p in sorted(r.rglob("*")):
                if (p.is_file() and "__pycache__" not in p.parts
                        and p.suffix != ".pyc"):
                    snap[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


class TestTwoScopePurity(unittest.TestCase):
    def test_per_repo_fill_mutates_zero_claude_source_config(self):
        # AC (two-scope): a per-repo apply of ALL three concerns writes only inside
        # the target repo — zero ~/.claude source/config mutation (bytecode excepted).
        repo = _new_repo(_recorded(["agents-md", "status-harness", "merge-gate"]))
        before = _global_snapshot()
        auto_fill.apply(repo)
        after = _global_snapshot()
        self.assertEqual(before, after,
                         "per-repo fill mutated ~/.claude source/config: "
                         f"{set(before) ^ set(after) or {k for k in before if before[k] != after.get(k)}}")
        # sanity: the repo itself WAS filled (so the test isn't vacuous)
        self.assertTrue((repo / "AGENTS.md").is_file())
        self.assertTrue((repo / ".git" / "hooks" / "pre-push").is_file())


def _run_cli(argv):
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = auto_fill.main(argv)
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _home(path: Path):
    """Redirect HOME so the setup skills' Path.home()-derived write targets land
    under a throwaway dir — the only safe way to exercise global writes without
    touching the operator's real ~/.claude. import_setup re-imports the modules
    fresh each call, so module-level Path.home() re-evaluates under this HOME."""
    orig = os.environ.get("HOME")
    os.environ["HOME"] = str(path)
    try:
        yield
    finally:
        if orig is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = orig


def _snapshot_dir(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            snap[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


class TestGlobalBootstrap(unittest.TestCase):
    def test_global_bootstrap_installs_status_harness_global_layer(self):
        # AC (global-bootstrap): the one-time global path installs the status-harness
        # global layer (status.py + SessionStart/Stop hooks) under ~/.claude.
        fake_home = Path(tempfile.mkdtemp())
        with _home(fake_home):
            result = auto_fill.global_bootstrap()
        self.assertEqual(result["status"], "applied")
        self.assertTrue((fake_home / ".claude" / "scripts" / "status.py").is_file())
        settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for grp in settings["hooks"].get("SessionStart", [])
                for h in grp["hooks"]]
        self.assertTrue(any("status.py" in c for c in cmds))

    def test_global_bootstrap_is_content_checked_noop_when_in_place(self):
        # AC (global-bootstrap idempotent): a second run after the layer is in place
        # detects no pending work, applies nothing, and changes no bytes.
        fake_home = Path(tempfile.mkdtemp())
        with _home(fake_home):
            auto_fill.global_bootstrap()                      # first run installs
            before = _snapshot_dir(fake_home / ".claude")
            result = auto_fill.global_bootstrap()             # second run
            after = _snapshot_dir(fake_home / ".claude")
        self.assertEqual(result["status"], "noop")
        self.assertEqual(before, after)

    def test_build_plan_surfaces_global_scope_record_when_pending(self):
        # AC (two scopes): when the operator-global layer is pending, build_plan
        # surfaces a single scope='global' record (so the skill offers the one-time
        # bootstrap), and the per-repo apply() NEVER performs the global writes.
        fake_home = Path(tempfile.mkdtemp())           # fresh -> global layer absent
        repo = _new_repo(_recorded(["status-harness"]))
        with _home(fake_home):
            plan = auto_fill.build_plan(repo)
            glob = [r for r in plan["records"] if r["scope"] == "global"]
            self.assertEqual(len(glob), 1)             # ONE batched global record
            auto_fill.apply(repo)                       # repo auto tier only
        self.assertFalse((fake_home / ".claude" / "scripts" / "status.py").exists())

    def test_no_global_record_for_scaffold_without_global_concern(self):
        # Post-impl F3: a scaffold with no global-dimension concern (agents-md only)
        # must NOT surface a global-bootstrap record, even on a fresh HOME — global
        # setup is only relevant when status-harness or merge-gate is in scope.
        fake_home = Path(tempfile.mkdtemp())
        repo = _new_repo(_recorded(["agents-md"]))
        with _home(fake_home):
            plan = auto_fill.build_plan(repo)
        self.assertEqual([r for r in plan["records"] if r["scope"] == "global"], [])

    def test_global_bootstrap_blocked_on_invalid_settings_json(self):
        # AC (structured failure): an invalid ~/.claude/settings.json yields a
        # structured 'blocked' status (not a crash, not a silent half-write).
        fake_home = Path(tempfile.mkdtemp())
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "settings.json").write_text("{ not valid json",
                                                             encoding="utf-8")
        with _home(fake_home):
            result = auto_fill.global_bootstrap()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("settings.json", result["reason"])

    def test_global_bootstrap_blocked_when_write_fails_after_clean_detect(self):
        # Post-impl F1: the WRITE-time failure branch (read-only detect clean, the
        # apply write fails — a TOCTOU) returns a structured 'blocked', never a
        # half-write. Simulated by a stub status-harness whose install_global passes
        # detect (apply=False) but fails apply (apply=True).
        fake_home = Path(tempfile.mkdtemp())
        (fake_home / ".claude").mkdir(parents=True)

        class _FakeSH:
            SETTINGS = fake_home / ".claude" / "settings.json"

            @staticmethod
            def install_global(actions, apply):
                if apply:
                    return False                                   # write fails
                actions.append(("change", "install status.py"))    # detect: pending, clean
                return True

        orig = harness_doctor.import_setup
        harness_doctor.import_setup = (
            lambda skill, module: _FakeSH if skill == "setup-status-harness"
            else orig(skill, module))
        try:
            result = auto_fill.global_bootstrap()
        finally:
            harness_doctor.import_setup = orig
        self.assertEqual(result["status"], "blocked")

    def test_status_harness_fills_from_bundled_when_global_absent(self):
        # Post-impl F2 (rigor): with the GLOBAL status.py absent but the bundled
        # snapshot present, the REAL vendor-source guard is True and status-harness
        # fills from bundled — not report-only.
        fake_home = Path(tempfile.mkdtemp())
        repo = _new_repo(_recorded(["status-harness"]))
        with _home(fake_home):
            self.assertFalse((fake_home / ".claude" / "scripts" / "status.py").exists())
            auto_fill.apply(repo)
        self.assertTrue((repo / "scripts" / "status.py").is_file())   # vendored from bundled


class TestReportOnlyConcerns(unittest.TestCase):
    def test_orphan_recorded_slug_is_report_only_not_silently_dropped(self):
        # AC (inputs): a recorded scaffold slug with no auto-fill path (the
        # merge-gate-local slug trap / a hand-edit) is SURFACED report-only, never
        # silently dropped and never filled.
        repo = _new_repo(_recorded(["merge-gate-local"]))
        plan = auto_fill.build_plan(repo)
        orphan = [r for r in plan["records"] if r["concern"] == "merge-gate-local"]
        self.assertEqual([r["consent_tier"] for r in orphan], ["refuse"])
        self.assertEqual(auto_fill.apply(repo)["applied"], [])


class TestPlanContract(unittest.TestCase):
    def test_no_record_carries_a_callable(self):
        # AC (record shape): no Python callable crosses the script↔skill seam — every
        # record value is JSON-able (refuse/parked records carry no invokable apply).
        repo = _new_repo(_recorded(["agents-md", "merge-gate", "merge-gate-local"]))
        (repo / "CLAUDE.md").write_text("real content\n", encoding="utf-8")  # migrate
        for r in auto_fill.build_plan(repo)["records"]:
            for v in r.values():
                self.assertFalse(callable(v), f"record carries a callable: {r}")

    def test_plan_is_json_serializable(self):
        # AC (plan transport): the whole plan survives json.dumps (no callable).
        repo = _new_repo(_recorded(["agents-md", "status-harness", "merge-gate",
                                    "merge-gate-local"]))
        json.dumps(auto_fill.build_plan(repo))      # must not raise

    def test_apply_confirmed_runs_a_confirm_tier_migrate_by_id(self):
        # AC (staged API): SKILL.md runs the go-ahead, then calls apply_confirmed
        # with the confirmed action_ids — which re-derives from repo state (no
        # callable persisted) and applies the confirm-tier work for those concerns.
        repo = _new_repo(_recorded(["agents-md"]))
        original = "# real CLAUDE content to migrate\n"
        (repo / "CLAUDE.md").write_text(original, encoding="utf-8")  # State-2 migrate
        ids = [r["action_id"] for r in auto_fill.build_plan(repo)["records"]
               if r["consent_tier"] == "confirm"]
        self.assertTrue(ids)
        auto_fill.apply_confirmed(repo, ids)
        self.assertTrue((repo / "AGENTS.md").is_file())                        # migrated
        self.assertIn("real CLAUDE content", (repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("@AGENTS.md", (repo / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_duplicated_scaffold_slug_is_deduped(self):
        # Post-impl F2: a hand-edited [harness] with a repeated slug must not produce
        # duplicate plan records (colliding action_ids) or run an installer twice —
        # mirrors compute_coverage's dedup.
        repo = _new_repo(_recorded(["merge-gate", "merge-gate"]))
        plan = auto_fill.build_plan(repo)
        mg = [r for r in plan["records"] if r["concern"] == "merge-gate"]
        self.assertEqual(len(mg), 1)
        self.assertEqual(auto_fill.apply(repo)["applied"].count("merge-gate"), 1)

    def test_error_action_is_report_only_not_raw_error_kind(self):
        # Post-impl F3: an 'error' action (e.g. agents-md templates missing) maps to a
        # report-only record (kind='report'), agreeing with the engine's intent='n/a'
        # — not a raw kind='error' record in the refuse/report vocabulary the skill uses.
        recs = auto_fill._action_records("agents-md", "repo",
                                         [("error", "template files missing", None)])
        self.assertEqual([r["kind"] for r in recs], ["report"])
        self.assertEqual([r["consent_tier"] for r in recs], ["refuse"])

    def test_gap_outside_recorded_scaffold_is_not_filled(self):
        # AC (inputs): fillable = diagnose gaps ∩ recorded scaffold. status-harness
        # is a gap here but NOT in the scaffold -> it must not be filled.
        repo = _new_repo(_recorded(["agents-md"]))
        auto_fill.apply(repo)
        self.assertTrue((repo / "AGENTS.md").is_file())              # in scaffold
        self.assertFalse((repo / "scripts" / "status.py").exists())  # gap, not in scaffold


class TestIdempotency(unittest.TestCase):
    def test_per_repo_fill_is_idempotent(self):
        # AC (idempotency): a re-run after a successful fill applies ZERO writes —
        # checked against the apply summary, the second-run plan, and the tree.
        repo = _new_repo(_recorded(["agents-md", "status-harness", "merge-gate"]))
        auto_fill.apply(repo)                       # first fill
        snap1 = _snapshot_dir(repo)
        res2 = auto_fill.apply(repo)                # second run
        snap2 = _snapshot_dir(repo)
        self.assertEqual(res2["applied"], [])       # nothing applied the second time
        self.assertEqual(snap1, snap2)              # tree byte-identical
        plan2 = auto_fill.build_plan(repo)
        auto = [r for r in plan2["records"]
                if r["scope"] == "repo" and r["consent_tier"] == "auto"]
        self.assertEqual(auto, [])                  # no auto-tier work left


class TestCli(unittest.TestCase):
    def test_json_emits_the_plan(self):
        # AC (plan transport): the skill drives auto_fill via the CLI; --json prints
        # the JSON-serializable plan and exits 0.
        repo = _new_repo(_recorded(["agents-md"]))
        code, out, err = _run_cli([str(repo), "--json"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertTrue(plan["profile_recorded"])
        self.assertTrue(any(r["concern"] == "agents-md" for r in plan["records"]))

    def test_apply_fills_the_auto_tier(self):
        # AC: --apply applies the repo auto tier (the same path apply() takes).
        repo = _new_repo(_recorded(["agents-md"]))
        code, _, _ = _run_cli([str(repo), "--apply"])
        self.assertEqual(code, 0)
        self.assertTrue((repo / "AGENTS.md").is_file())

    def test_cli_runs_as_a_standalone_subprocess(self):
        # The skill invokes auto_fill.py as a subprocess; it must resolve
        # harness_doctor (in ~/.claude/scripts) on its OWN — no caller-set sys.path.
        repo = _new_repo(_recorded(["agents-md"]))
        r = subprocess.run([sys.executable, str(_HERE / "auto_fill.py"),
                            str(repo), "--json"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertTrue(plan["profile_recorded"])

    def test_subdir_invocation_resolves_to_the_git_root(self):
        # Post-impl F5: a subdir path must resolve to the git ROOT — the recorded
        # [harness] + the agents-md target both live at the root, so AGENTS.md is
        # created at the root, never silently no-op'd or written into the subdir.
        repo = _new_repo(_recorded(["agents-md"]))
        sub = repo / "src" / "auth"
        sub.mkdir(parents=True)
        auto_fill.apply(sub)
        self.assertTrue((repo / "AGENTS.md").is_file())     # created at the git root
        self.assertFalse((sub / "AGENTS.md").exists())      # NOT in the subdir


class TestStatusHarnessFill(unittest.TestCase):
    def test_greenfield_status_harness_is_auto_filled(self):
        # AC (delegation): status-harness is delegated to install_project(apply=True)
        # — its writes happen inline (no per-action apply_fn). A greenfield repo gets
        # the vendored status.py + the regen workflow (all additive = auto tier).
        repo = _new_repo(_recorded(["status-harness"]))
        auto_fill.apply(repo)
        self.assertTrue((repo / "scripts" / "status.py").is_file())
        self.assertTrue((repo / ".github" / "workflows" / "regen-status.yml").is_file())

    def test_mixed_tier_change_auto_and_diverged_warn_refuse(self):
        # AC (per-action tiers mix within a concern): a DIVERGED vendored status.py
        # (warn -> refuse) sits beside missing sub-files (change -> auto). The plan
        # carries BOTH tiers for status-harness; apply() applies the change
        # sub-files and leaves the diverged file byte-untouched (the correct
        # partial delegation — install_project never overwrites a warn file).
        repo = _new_repo(_recorded(["status-harness"]))
        (repo / "scripts").mkdir()
        diverged = "# my customized status.py — must not be clobbered\n"
        (repo / "scripts" / "status.py").write_text(diverged, encoding="utf-8")
        plan = auto_fill.build_plan(repo)
        tiers = {r["consent_tier"] for r in plan["records"]
                 if r["concern"] == "status-harness"}
        self.assertIn("auto", tiers)        # missing workflow/doc/gitignore
        self.assertIn("refuse", tiers)      # diverged status.py
        auto_fill.apply(repo)
        self.assertEqual((repo / "scripts" / "status.py").read_text(encoding="utf-8"),
                         diverged)           # diverged file preserved
        self.assertTrue((repo / ".github" / "workflows" / "regen-status.yml").is_file())

    def test_vendor_source_guard_only_false_when_neither_source_readable(self):
        # AC (vendor source): the guard is False only when NEITHER the global
        # status.py NOR the bundled snapshot exists.
        class _Neither:
            STATUS_PY = Path("/nonexistent/status.py")
            BUNDLED = Path("/nonexistent/bundled.py")

        class _BundledOnly:
            STATUS_PY = Path("/nonexistent/status.py")
            BUNDLED = Path(__file__)            # a real, readable file

        self.assertFalse(auto_fill._status_vendor_source_ok(_Neither))
        self.assertTrue(auto_fill._status_vendor_source_ok(_BundledOnly))

    def test_missing_vendor_source_is_report_only_not_a_crash(self):
        # AC (vendor source): with no readable vendor source, status-harness is
        # report-only — build_plan/apply must NOT raise (install_project reads the
        # source at its top even with apply=False) and must write nothing.
        repo = _new_repo(_recorded(["status-harness"]))
        orig = auto_fill._status_vendor_source_ok
        auto_fill._status_vendor_source_ok = lambda mod: False   # simulate missing source
        try:
            plan = auto_fill.build_plan(repo)
            res = auto_fill.apply(repo)                           # must not raise
        finally:
            auto_fill._status_vendor_source_ok = orig
        self.assertNotIn("status-harness", res["applied"])
        self.assertFalse((repo / "scripts" / "status.py").exists())
        sh = [r for r in plan["records"] if r["concern"] == "status-harness"]
        self.assertTrue(sh and all(r["consent_tier"] == "refuse" for r in sh))


if __name__ == "__main__":
    unittest.main()
