#!/usr/bin/env python3
"""Tests for harness_doctor.py — the Tier-1 read-only scaffold-conformance engine.

Read-only by contract (scaffold-doctor #02 / ADR-0020): every path here asserts
behaviour through the public interface (`find_repo_root`, `diagnose`, `main`) and
must never mutate a scanned repo. Uses unittest to match the repo's other suites.
"""
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import harness_doctor


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = harness_doctor.main(argv)
    return code, out.getvalue(), err.getvalue()


def _snapshot(root: Path) -> dict:
    """relpath -> (size, sha256) for every file under root (incl. .git)."""
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            blob = p.read_bytes()
            snap[str(p.relative_to(root))] = (len(blob), hashlib.sha256(blob).hexdigest())
    return snap


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _new_repo(toml: str = None) -> Path:
    """A fresh git repo dir, optionally seeded with a harness.toml."""
    d = Path(tempfile.mkdtemp())
    _git_init(d)
    if toml is not None:
        (d / "harness.toml").write_text(toml, encoding="utf-8")
    return d


def _record(records, concern):
    """The single record for `concern`, or None."""
    found = [r for r in records if r["concern"] == concern]
    return found[0] if found else None


LOCAL_TOML = '[merge-gate]\nprofile = "local"\n\n[merge-gate.local]\nenforcement_policy = "advisory"\n'

# Verbatim shape of the real legacy github-actions target repos (e.g.
# chess_transformer): [merge-gate] with soft_mode_default / codex_review_cmd /
# bypass_label, NO profile, NO [merge-gate.local].
LEGACY_GHA_TOML = """[merge-gate]
project_name = 'chess_transformer'
soft_mode_default = 'true'
docs_only_globs = ['*.md', 'docs/**', '.scratch/**/*.md', 'LICENSE', 'NOTICE']
node_version = '20'
codex_install_cmd = 'npm install -g @openai/codex@latest'
codex_review_cmd = 'codex exec --json --output-schema .codex-review/schema.json "Run an adversarial review"'
bypass_label = 'merge-gate-bypass'
"""


class TestFindRepoRoot(unittest.TestCase):
    def test_returns_none_outside_a_git_repo(self):
        d = Path(tempfile.mkdtemp())
        self.assertIsNone(harness_doctor.find_repo_root(d))

    def test_returns_toplevel_inside_a_git_repo(self):
        d = Path(tempfile.mkdtemp())
        _git_init(d)
        # resolve() because macOS/WSL tmp may be a symlink; git reports the real path
        self.assertEqual(harness_doctor.find_repo_root(d), Path(d).resolve())


class TestMergeGateIntent(unittest.TestCase):
    def test_local_profile_toml_reports_intent_present(self):
        repo = _new_repo(LOCAL_TOML)
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["intent"], "present")


def _write_hook(repo: Path, name: str, body: str) -> None:
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / name).write_text(body, encoding="utf-8")


class TestMergeGateEnforcement(unittest.TestCase):
    def test_fresh_clone_intent_present_enforcement_unwired(self):
        # The canonical state: committed config, but no local pre-push hook
        # (a fresh clone has no hooks). Legitimate, not a defect.
        repo = _new_repo(LOCAL_TOML)
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertEqual(rec["intent"], "present")
        self.assertEqual(rec["enforcement"], "unwired")

    def test_pre_push_hook_carrying_marker_is_wired(self):
        repo = _new_repo(LOCAL_TOML)
        _write_hook(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\nexit 0\n")
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertEqual(rec["enforcement"], "wired")

    def test_foreign_pre_push_hook_without_marker_is_unwired(self):
        # A husky/secret-scan pre-push exists but is not ours → still unwired.
        repo = _new_repo(LOCAL_TOML)
        _write_hook(repo, "pre-push", "#!/bin/sh\nnpx husky\nexit 0\n")
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertEqual(rec["enforcement"], "unwired")

    def test_measurement_post_commit_is_a_merge_gate_detail_not_a_concern(self):
        # #33 measurement wiring is folded into merge-gate's detail; the enforcement
        # verdict still keys on pre-push (the gate), not post-commit (the producer).
        repo = _new_repo(LOCAL_TOML)
        _write_hook(repo, "post-commit", "#!/bin/sh\n# MERGE_GATE_POST_COMMIT\n")
        recs = harness_doctor.diagnose(repo)
        self.assertIsNone(_record(recs, "measurement"))   # never its own concern
        mg = _record(recs, "merge-gate")
        self.assertEqual(mg["enforcement"], "unwired")     # keys on pre-push only
        self.assertIn("measurement", mg["detail"].lower())  # surfaced as detail


SELF_VERIFY_TOML = ('[self-verification]\ntest = "python3 -m pytest -q"\n'
                    'bypass_trailer = "Self-Verify-Bypass"\n')


class TestSelfVerificationParked(unittest.TestCase):
    def test_present_is_parked_and_probed_unwired_without_hook(self):
        repo = _new_repo(SELF_VERIFY_TOML)
        rec = _record(harness_doctor.diagnose(repo), "self-verification")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["intent"], "present")
        self.assertEqual(rec["applicability"], "parked")  # dormant per ADR
        self.assertEqual(rec["enforcement"], "unwired")   # honestly probed

    def test_commit_msg_marker_probed_wired_yet_still_parked(self):
        repo = _new_repo(SELF_VERIFY_TOML)
        _write_hook(repo, "commit-msg", "#!/bin/sh\n# SELF_VERIFICATION_COMMIT_MSG\n")
        rec = _record(harness_doctor.diagnose(repo), "self-verification")
        self.assertEqual(rec["enforcement"], "wired")
        self.assertEqual(rec["applicability"], "parked")


class TestReadOnlyInvariant(unittest.TestCase):
    def test_diagnose_mutates_no_file_on_any_path(self):
        states = {
            "bare": None,
            "local": LOCAL_TOML,
            "legacy-gha": LEGACY_GHA_TOML,
            "self-verify": SELF_VERIFY_TOML,
            "unknown-gate": '[deploy-gate]\nprofile = "local"\n',
        }
        for name, toml in states.items():
            repo = _new_repo(toml)
            (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
            _write_hook(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
            with self.subTest(state=name):
                before = _snapshot(repo)
                harness_doctor.diagnose(repo)
                self.assertEqual(_snapshot(repo), before)


class TestUnknownClass(unittest.TestCase):
    def test_declared_gate_absent_from_registry_is_unknown_class(self):
        # A future gate following the section convention but not yet registered
        # must surface (so you notice to register it), never be silently dropped.
        repo = _new_repo('[deploy-gate]\nprofile = "local"\n')
        rec = _record(harness_doctor.diagnose(repo), "deploy-gate")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["state"], "unknown-class")
        self.assertEqual(rec["enforcement"], "n/a")

    def test_harness_meta_section_is_not_treated_as_a_gate(self):
        # [harness] is the ADR-0020 §3 intended-profile meta (#03), not a gate.
        repo = _new_repo('[harness]\nmerge-gate = "local"\n')
        self.assertIsNone(_record(harness_doctor.diagnose(repo), "harness"))


class TestLegacyGhaSchema(unittest.TestCase):
    def test_legacy_gha_is_needs_migration_not_local_with_missing_hook(self):
        # The disaster guarded against: misreading this as a local gate with an
        # absent hook would let #04 auto-install a local pre-push onto a GHA repo.
        repo = _new_repo(LEGACY_GHA_TOML)
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertEqual(rec["intent"], "present")
        self.assertEqual(rec["state"], "legacy-schema/needs-migration")
        self.assertNotEqual(rec["enforcement"], "unwired")  # never installable-local
        self.assertEqual(rec["applicability"], "parked")     # GHA frozen, ADR-0009


class TestUnrecognizedProfile(unittest.TestCase):
    # Legacy must be recognized POSITIVELY (GHA keys + no profile), not by
    # "!= local" — else a typo'd profile or an empty section is silently blessed
    # as parked legacy and the doctor falsely reports "Scaffold conforms" (#02 AC).
    def test_typo_profile_is_surfaced_as_a_gap_not_blessed_as_legacy(self):
        repo = _new_repo('[merge-gate]\nprofile = "locl"\n')  # typo of "local"
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertNotEqual(rec["state"], "legacy-schema/needs-migration")
        self.assertNotEqual(rec["applicability"], "parked")  # not false-blessed
        self.assertTrue(harness_doctor._is_gap(rec))          # surfaced (exit 2)

    def test_empty_merge_gate_section_is_surfaced_as_a_gap(self):
        repo = _new_repo("[merge-gate]\n")  # present but empty: no profile, no keys
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertNotEqual(rec["state"], "legacy-schema/needs-migration")
        self.assertTrue(harness_doctor._is_gap(rec))

    def test_github_actions_profile_is_recognized_and_parked(self):
        # The current-schema dormant GHA profile (ADR-0009) is recognized, parked,
        # and NOT a gap — distinct from the legacy pre-profile schema.
        repo = _new_repo('[merge-gate]\nprofile = "github-actions"\n')
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertEqual(rec["applicability"], "parked")
        self.assertFalse(harness_doctor._is_gap(rec))


class TestReuseByImport(unittest.TestCase):
    def test_agents_md_absent_in_bare_repo(self):
        # Detected by REUSING setup_agents_md.plan(), not a re-implemented regex.
        repo = _new_repo()  # no AGENTS.md / CLAUDE.md
        rec = _record(harness_doctor.diagnose(repo), "agents-md")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["intent"], "absent")

    def test_agents_md_present_when_wired(self):
        repo = _new_repo()
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        rec = _record(harness_doctor.diagnose(repo), "agents-md")
        self.assertEqual(rec["intent"], "present")
        self.assertEqual(rec["enforcement"], "n/a")  # in-tree concern, no hook

    def test_status_harness_absent_in_bare_repo(self):
        # Detected by REUSING setup_status_harness.install_project(apply=False).
        repo = _new_repo()
        rec = _record(harness_doctor.diagnose(repo), "status-harness")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["intent"], "absent")


class TestCli(unittest.TestCase):
    def test_main_exits_1_outside_a_git_repo(self):
        d = Path(tempfile.mkdtemp())  # not a git repo
        code, _out, _err = _run_main([str(d)])
        self.assertEqual(code, 1)

    def test_main_exits_2_when_applicable_concerns_have_gaps(self):
        repo = _new_repo()  # bare → agents-md/status-harness/merge-gate all absent
        code, _out, _err = _run_main([str(repo)])
        self.assertEqual(code, 2)

    def test_main_exits_0_on_a_fully_conforming_repo(self):
        repo = _new_repo(LOCAL_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _write_hook(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
        # install status-harness for real (apply=True) so it is genuinely present
        sh = harness_doctor.import_setup("setup-status-harness", "setup_status_harness")
        sh.install_project(repo, [], True)
        code, out, _err = _run_main([str(repo)])
        self.assertEqual(code, 0, msg=out)

    def test_json_report_lists_concerns_and_exit(self):
        repo = _new_repo(LOCAL_TOML)
        code, out, _err = _run_main([str(repo), "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["exit"], code)
        self.assertEqual(payload["repo"], str(Path(repo).resolve()))
        concerns = {c["concern"] for c in payload["concerns"]}
        self.assertIn("merge-gate", concerns)
        self.assertIn("agents-md", concerns)
        # every record carries the full two-axis shape
        for c in payload["concerns"]:
            self.assertEqual(set(c), {"concern", "intent", "enforcement",
                                      "applicability", "state", "detail"})


def _add_remote(repo: Path, url: str = "https://example.com/x.git") -> None:
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)


class TestProposeProfile(unittest.TestCase):
    def test_bare_git_repo_proposes_base_doc_structure_scaffold(self):
        # propose_profile is the read-only candidate-set function (#03 AC1). The
        # doc/structure scaffold (agents-md + status-harness) is the base any git
        # repo gets — no judgment row (AC2).
        repo = _new_repo()
        profile = harness_doctor.propose_profile(repo)
        self.assertIn("agents-md", profile["scaffold"])
        self.assertIn("status-harness", profile["scaffold"])


    def test_test_suite_surfaces_self_verification_candidate(self):
        # A test suite surfaces the dormant self-verification opt-in row (AC5).
        # Bounded heuristic (documented imprecision): a tests/|test/ dir or a
        # recognized test-file name. A repo with code but no recognized test
        # layout does NOT propose it (the absent branch is pinned, not just present).
        with_tests = _new_repo()
        (with_tests / "tests").mkdir()
        (with_tests / "tests" / "test_core.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        self.assertIn("self-verification",
                      harness_doctor.propose_profile(with_tests)["scaffold"])
        no_tests = _new_repo()
        (no_tests / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (no_tests / "main.py").write_text("print(1)\n", encoding="utf-8")
        self.assertNotIn("self-verification",
                         harness_doctor.propose_profile(no_tests)["scaffold"])

    def test_build_manifest_surfaces_ci_recommendation(self):
        # Buildable code (a build manifest) surfaces the CI judgment row (AC4).
        # ci is a recommendation flag, NEVER a scaffold member (no detector → it
        # would otherwise nag forever); the judgment row records the real answer.
        bare = _new_repo()
        bare_prop = harness_doctor.propose_profile(bare)
        self.assertFalse(bare_prop["ci"])
        self.assertNotIn("ci", bare_prop["scaffold"])
        buildable = _new_repo()
        (buildable / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        self.assertTrue(harness_doctor.propose_profile(buildable)["ci"])

    def test_git_remote_surfaces_merge_gate_candidate(self):
        # A remote is the signal merge review may be wanted (local profile =
        # trusted self-authored diffs; the remote is what proposes the gate). AC3.
        # wanted ≠ applicable, so it is a *candidate* the judgment row confirms.
        bare = _new_repo()
        self.assertNotIn("merge-gate", harness_doctor.propose_profile(bare)["scaffold"])
        remote = _new_repo()
        _add_remote(remote)
        self.assertIn("merge-gate", harness_doctor.propose_profile(remote)["scaffold"])


def _install_status_harness(repo: Path) -> None:
    sh = harness_doctor.import_setup("setup-status-harness", "setup_status_harness")
    sh.install_project(repo, [], True)


class TestComputeCoverage(unittest.TestCase):
    def test_fraction_counts_present_concerns_over_applicable(self):
        # Coverage = |installed ∩ (scaffold\parked)| ÷ |scaffold\parked|, where
        # installed = intent==present (the INTENT axis; enforcement wired/unwired
        # is phase-2, AC12). A repo with all three base+gate concerns present →
        # fully covered, regardless of whether the pre-push hook is wired.
        repo = _new_repo(LOCAL_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _install_status_harness(repo)
        records = harness_doctor.diagnose(repo)
        cov = harness_doctor.compute_coverage(
            records, {"scaffold": ["agents-md", "status-harness", "merge-gate"], "ci": True})
        self.assertEqual(cov["fraction"], (3, 3))
        self.assertEqual(set(cov["covered"]), {"agents-md", "status-harness", "merge-gate"})
        self.assertEqual(set(cov["applicable"]), {"agents-md", "status-harness", "merge-gate"})


    def test_parked_self_verification_excluded_but_wanted_gha_merge_gate_stays(self):
        # AC12 + two panel blockers. (a) self-verification is the one
        # legitimately-parked-in-scaffold concern (no installer) → out of the
        # denominator, surfaced as "opted-in, parked". (b) A merge-gate the
        # operator WANTED but that resolves to a frozen github-actions profile
        # (record applicability=parked) must NOT vanish from the denominator —
        # parked is the static no-installer set, NOT "any parked record". Its
        # intent is present, so it counts installed (the migration need is #02's
        # needs-migration state, not the phase-1 fraction).
        repo = _new_repo('[merge-gate]\nprofile = "github-actions"\n\n' + SELF_VERIFY_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _install_status_harness(repo)
        records = harness_doctor.diagnose(repo)
        cov = harness_doctor.compute_coverage(records, {
            "scaffold": ["agents-md", "status-harness", "merge-gate", "self-verification"],
            "ci": False})
        self.assertNotIn("self-verification", cov["applicable"])      # parked → out of denom
        self.assertIn("self-verification", cov["parked_opted_in"])    # surfaced as opted-in/parked
        self.assertIn("merge-gate", cov["applicable"])                # wanted GHA gate STAYS in denom
        self.assertIn("merge-gate", cov["covered"])                   # intent=present → installed

    def test_unrecognized_scaffold_slug_is_surfaced_not_a_perpetual_gap(self):
        # AC11 trap at the coverage layer + panel blocker-1. A recorded scaffold
        # slug with no diagnose record and not in SCAFFOLD_CONCERNS (the classic
        # "merge-gate-local" mistake, or a hand-edited/future slug) must be
        # surfaced as unrecognized — NEVER folded into the denominator where it
        # would be an uncloseable false gap (coverage stuck < 100% forever).
        repo = _new_repo()  # bare → agents-md/status-harness/merge-gate records
        cov = harness_doctor.compute_coverage(harness_doctor.diagnose(repo), {
            "scaffold": ["agents-md", "status-harness", "merge-gate-local"], "ci": True})
        self.assertIn("merge-gate-local", cov["unrecognized"])
        self.assertNotIn("merge-gate-local", cov["applicable"])
        self.assertEqual(set(cov["applicable"]), {"agents-md", "status-harness"})

    def test_empty_scaffold_is_not_measurable_no_zero_division(self):
        # A malformed/partial recorded [harness] (no scaffold, or scaffold=[])
        # yields a 0 denominator. compute_coverage must flag it not-measurable
        # rather than divide by zero or report a vacuous "0/0 conforms" (panel).
        repo = _new_repo()
        cov = harness_doctor.compute_coverage(harness_doctor.diagnose(repo),
                                              {"scaffold": [], "ci": True})
        self.assertFalse(cov["measurable"])
        self.assertEqual(cov["fraction"], (0, 0))

    def test_duplicate_scaffold_slugs_do_not_inflate_the_fraction(self):
        # Coverage is set-valued (|scaffold\parked|). A hand-edited [harness]
        # with a repeated slug must not double-count: agents-md present → 1/1,
        # not 2/2 (impl review finding).
        repo = _new_repo()
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        cov = harness_doctor.compute_coverage(
            harness_doctor.diagnose(repo), {"scaffold": ["agents-md", "agents-md"], "ci": False})
        self.assertEqual(cov["fraction"], (1, 1))
        self.assertEqual(cov["applicable"], ["agents-md"])

    def test_malformed_record_without_concern_key_does_not_crash(self):
        # compute_coverage is a public import boundary (CI + phase-2 per ADR-0020
        # §6). A record lacking the 'concern' key must be skipped, never raise
        # KeyError (impl review finding) — matching the file's defensive idiom.
        cov = harness_doctor.compute_coverage(
            [{"intent": "present"}, {"concern": "agents-md", "intent": "present"}],
            {"scaffold": ["agents-md"], "ci": False})
        self.assertEqual(cov["covered"], ["agents-md"])

    def test_judgments_ci_reflects_recorded_value_never_live_signal(self):
        # AC6/AC12: ci is the recorded judgment ("wanted / not-yet-measurable"),
        # NOT re-derived from the build manifest at coverage time, and never a
        # term in the fraction. Repo HAS a manifest but recorded ci=False → the
        # report shows the recorded False, and ci is in neither applicable nor covered.
        repo = _new_repo()
        (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        cov = harness_doctor.compute_coverage(harness_doctor.diagnose(repo),
                                              {"scaffold": ["agents-md", "status-harness"], "ci": False})
        self.assertEqual(cov["judgments"]["ci"], False)
        self.assertNotIn("ci", cov["applicable"])
        self.assertNotIn("ci", cov["covered"])


class TestNewReadPathsReadOnly(unittest.TestCase):
    def test_propose_read_and_coverage_mutate_no_file(self):
        # AC1/AC15: propose_profile, read_recorded_profile and compute_coverage
        # are read-only. Regression-lock the zero-mutation invariant for #03's
        # paths (the engine's load-bearing safety property), not just #02's diagnose.
        states = {
            "bare": None,
            "local": LOCAL_TOML,
            "recorded": '[harness]\nscaffold = ["agents-md", "merge-gate"]\nci = true\n',
            "self-verify": SELF_VERIFY_TOML,
        }
        for name, toml in states.items():
            repo = _new_repo(toml)
            (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
            _add_remote(repo)
            with self.subTest(state=name):
                before = _snapshot(repo)
                prof = harness_doctor.propose_profile(repo)
                rec = harness_doctor.read_recorded_profile(repo)
                harness_doctor.compute_coverage(harness_doctor.diagnose(repo), rec or prof)
                self.assertEqual(_snapshot(repo), before)


class TestReadRecordedProfile(unittest.TestCase):
    def test_reads_recorded_scaffold_and_ci(self):
        # Subsequent runs read the recorded [harness] profile (AC13 precondition).
        repo = _new_repo('[harness]\nscaffold = ["agents-md", "merge-gate"]\nci = true\n')
        prof = harness_doctor.read_recorded_profile(repo)
        self.assertEqual(prof["scaffold"], ["agents-md", "merge-gate"])
        self.assertEqual(prof["ci"], True)

    def test_returns_none_when_no_harness_section(self):
        # No [harness] → None → first run; the skill proposes, main shows raw
        # presence (AC14). A bare [merge-gate] is NOT a recorded profile.
        self.assertIsNone(harness_doctor.read_recorded_profile(_new_repo(LOCAL_TOML)))

    def test_missing_scaffold_key_defaults_to_empty_list(self):
        # A partial/hand-edited [harness] (judgment only, no scaffold) must not
        # KeyError the read-only engine (panel finding).
        prof = harness_doctor.read_recorded_profile(_new_repo('[harness]\nci = false\n'))
        self.assertEqual(prof["scaffold"], [])
        self.assertEqual(prof["ci"], False)

    def test_non_table_harness_value_is_no_profile_not_a_crash(self):
        # codex:finding-1: a hand-edited `harness = "x"` (a string, NOT a table)
        # must not AttributeError the read-only engine on `.get(...)`. A non-table
        # [harness] is not a recorded profile → None (the raw-presence path),
        # mirroring _load_toml's degrade-don't-raise idiom.
        self.assertIsNone(harness_doctor.read_recorded_profile(_new_repo('harness = "x"\n')))

    def test_non_list_scaffold_does_not_char_explode_into_slugs(self):
        # codex:finding-1: `scaffold = "agents-md"` (a string, not a list) must NOT
        # become list("agents-md") == ['a','g','e',…] — 9 garbage "unrecognized"
        # slugs. A non-list scaffold degrades to [], never a char-explosion.
        prof = harness_doctor.read_recorded_profile(
            _new_repo('[harness]\nscaffold = "agents-md"\n'))
        self.assertEqual(prof["scaffold"], [])

    def test_non_string_scaffold_entries_are_dropped(self):
        # A list with non-string entries keeps only the real slugs, so the
        # downstream diagnose-join / dedupe never sees a non-hashable-or-typed slug.
        prof = harness_doctor.read_recorded_profile(
            _new_repo('[harness]\nscaffold = ["agents-md", 3, "merge-gate"]\n'))
        self.assertEqual(prof["scaffold"], ["agents-md", "merge-gate"])

    def test_non_bool_ci_degrades_to_none(self):
        # codex:finding-1: `ci = "false"` (a string) must not render as wanted=yes
        # (a non-empty string is truthy). A non-bool ci degrades to None
        # (not-recorded), matching "ci is bool or absent".
        prof = harness_doctor.read_recorded_profile(_new_repo('[harness]\nci = "false"\n'))
        self.assertIsNone(prof["ci"])
        self.assertEqual(prof["scaffold"], [])


class TestScaffoldSlugContract(unittest.TestCase):
    def test_scaffold_concerns_are_exactly_diagnose_probe_ids(self):
        # AC11: scaffold slugs must be EXACTLY #02's probe IDs — a mismatch
        # silently empties installed∩scaffold and mis-computes coverage. Guard the
        # LIVE join (not a tautology over two literals): every SCAFFOLD_CONCERNS
        # slug must be a concern diagnose() actually emits, the merge-gate ≠
        # merge-gate-local trap must hold, and ci (a judgment, no probe) must NOT
        # be a scaffold concern.
        repo = _new_repo(LOCAL_TOML + "\n" + SELF_VERIFY_TOML)
        emitted = {r["concern"] for r in harness_doctor.diagnose(repo)}
        missing = set(harness_doctor.SCAFFOLD_CONCERNS) - emitted
        self.assertEqual(missing, set(), msg=f"scaffold slugs diagnose never emits: {missing}")
        self.assertIn("merge-gate", emitted)
        self.assertNotIn("merge-gate-local", emitted)
        self.assertNotIn("ci", harness_doctor.SCAFFOLD_CONCERNS)


class TestCliCoverage(unittest.TestCase):
    def test_json_includes_coverage_block_when_harness_recorded(self):
        # AC13: a recorded [harness] makes subsequent runs compute coverage
        # installed-vs-recorded. Here agents-md/status-harness absent, merge-gate
        # present → 1/3.
        repo = _new_repo(LOCAL_TOML + '\n[harness]\n'
                         'scaffold = ["agents-md", "status-harness", "merge-gate"]\nci = true\n')
        _code, out, _err = _run_main([str(repo), "--json"])
        payload = json.loads(out)
        self.assertIn("coverage", payload)
        self.assertEqual(payload["coverage"]["fraction"], [1, 3])
        self.assertIn("merge-gate", payload["coverage"]["covered"])

    def test_json_omits_coverage_when_no_harness_recorded(self):
        # AC14: no [harness] → no denominator → no coverage block (raw presence).
        _code, out, _err = _run_main([str(_new_repo(LOCAL_TOML)), "--json"])
        self.assertNotIn("coverage", json.loads(out))

    def test_human_output_renders_coverage_fraction_when_recorded(self):
        # AC13: the human table gains a coverage line when a profile is recorded.
        repo = _new_repo(LOCAL_TOML + '\n[harness]\n'
                         'scaffold = ["agents-md", "status-harness", "merge-gate"]\nci = true\n')
        _code, out, _err = _run_main([str(repo)])
        self.assertIn("coverage", out.lower())
        self.assertIn("1/3", out)   # merge-gate present, agents-md/status-harness absent

    def _conforming(self, extra=""):
        repo = _new_repo(LOCAL_TOML + extra)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _write_hook(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
        _install_status_harness(repo)
        return repo

    def test_exit_stays_keyed_on_02_gaps_recorded_or_not(self):
        # AC14/AC17: exit follows #02's gap rule, NOT coverage. Absence of
        # [harness] is not itself a gap (conforming repo → 0); recording a
        # [harness] adds no exit path (conforming repo → still 0); a real #02 gap
        # → 2 whether or not a [harness] is recorded.
        self.assertEqual(_run_main([str(self._conforming())])[0], 0)            # no [harness], conforming
        recorded = '\n[harness]\nscaffold = ["agents-md", "status-harness", "merge-gate"]\nci = true\n'
        self.assertEqual(_run_main([str(self._conforming(recorded))])[0], 0)    # recorded, conforming
        self.assertEqual(_run_main([str(_new_repo(LOCAL_TOML))])[0], 2)         # no [harness], gaps


class TestMainNeverProposes(unittest.TestCase):
    def test_main_does_not_invoke_propose_profile(self):
        # AC15/AC17/AC18: main() is a non-prompting reporter — proposal is the
        # skill's interactive-first-run-only job. Guard against a future edit
        # wiring propose_profile into main's no-[harness] branch (which would emit
        # a candidate set headlessly, blurring the write-ownership boundary).
        repo = _new_repo(LOCAL_TOML)  # no [harness] — the tempting branch
        called = []
        orig = harness_doctor.propose_profile
        harness_doctor.propose_profile = lambda *a, **k: (called.append(a), orig(*a, **k))[1]
        try:
            _run_main([str(repo)])
            _run_main([str(repo), "--json"])
            _run_main([str(repo), "--non-interactive"])
        finally:
            harness_doctor.propose_profile = orig
        self.assertEqual(called, [])


class TestHeadlessFlag(unittest.TestCase):
    def test_no_harness_interactive_hints_headless_suppresses(self):
        # AC18: on a first run with no recorded [harness], the interactive default
        # points the operator at the proposal interview; --non-interactive (the
        # explicit headless signal the skill passes) suppresses that hint. The
        # engine itself never prompts either way (it is plain, non-interactive).
        repo = _new_repo(LOCAL_TOML)  # no [harness]
        _code, out, _err = _run_main([str(repo)])
        self.assertIn("/harness-doctor", out)           # the proposal hint (≠ the "harness-doctor —" header)
        _code2, out2, _err2 = _run_main([str(repo), "--non-interactive"])
        self.assertNotIn("/harness-doctor", out2)

    def test_json_is_orthogonal_to_interactivity_and_never_hints(self):
        # AC18: headless is signaled EXPLICITLY, NOT inferred from --json. --json
        # is a machine report and never carries the interactive proposal hint.
        repo = _new_repo(LOCAL_TOML)
        _code, out, _err = _run_main([str(repo), "--json"])
        self.assertNotIn("/harness-doctor", out)


class TestReusedDetectorCrashDegrades(unittest.TestCase):
    def test_raising_detector_degrades_to_na_not_crash(self):
        # A reused detector that raises DURING detection (not at import) must
        # degrade the concern to intent="n/a", honoring the docstring contract
        # "never a false present/absent" — not crash the read-only doctor.
        # Trigger: CLAUDE.md is a directory, so setup_agents_md.plan()'s
        # claude.read_text() raises IsADirectoryError out of run(mod).
        repo = _new_repo()
        (repo / "CLAUDE.md").mkdir()
        records = harness_doctor.diagnose(repo)  # must not raise
        rec = _record(records, "agents-md")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["intent"], "n/a")


class TestHasTestSuiteSymlinks(unittest.TestCase):
    # claude:finding-1: the recursive scan used p.is_dir() (which FOLLOWS symlinks)
    # with prune-by-basename only, so a symlinked dir either escaped the repo
    # (scanning arbitrary FS for test-file names) or, pointing at an ancestor,
    # cycled forever — hanging propose_profile. The walk must not descend symlinks.
    def test_does_not_follow_symlinked_dir_out_of_repo(self):
        # A symlink to an OUTSIDE dir holding a test file must NOT be followed —
        # the heuristic is repo-scoped, so this repo (no in-tree tests) reads absent.
        outside = Path(tempfile.mkdtemp())
        (outside / "test_escape.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        repo = _new_repo()
        (repo / "src.py").write_text("x = 1\n", encoding="utf-8")  # a real dir entry to walk
        (repo / "link").symlink_to(outside, target_is_directory=True)
        self.assertFalse(harness_doctor._has_test_suite(repo))

    def test_symlink_cycle_terminates(self):
        # A self-referential symlink (loop -> repo root) must not be descended into,
        # so the bounded walk terminates instead of hanging (the guard makes it safe
        # to even run this test).
        repo = _new_repo()
        (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "loop").symlink_to(repo, target_is_directory=True)
        self.assertFalse(harness_doctor._has_test_suite(repo))


class TestMarkerRegistryContract(unittest.TestCase):
    """The marker fact has two owners by design: the engine's GATE_ENFORCEMENT /
    MEASUREMENT_HOOK registry (doctor side — what diagnose/auto_fill probe) and
    install_local's PRE_PUSH_MARKER / POST_COMMIT_MARKER (writer side — what the
    installed hooks carry). This test is the contract pinning them equal: if the
    writer renames a marker, the doctor must follow in the same change, or every
    wired repo silently reads as unwired."""

    def test_registry_markers_match_install_local(self):
        il = harness_doctor.import_setup("setup-merge-gate", "install_local")
        hook, marker = harness_doctor.GATE_ENFORCEMENT["merge-gate"]
        self.assertEqual(hook, "pre-push")
        self.assertEqual(marker, il.PRE_PUSH_MARKER)
        m_hook, m_marker = harness_doctor.MEASUREMENT_HOOK
        self.assertEqual(m_hook, "post-commit")
        self.assertEqual(m_marker, il.POST_COMMIT_MARKER)


if __name__ == "__main__":
    unittest.main()
