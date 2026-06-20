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
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    # +x to mirror a REAL installed hook (install_local chmods 0o755); the
    # enforcement-integrity check (ADR-0030 §5) now requires the executable bit,
    # so a "wired" fixture must carry it. The marked-but-non-executable case has
    # its own fixture (_write_hook_nonexec).
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    h = hooks / name
    h.write_text(body, encoding="utf-8")
    h.chmod(0o755)


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


class TestReadOnlyInvariant(unittest.TestCase):
    def test_diagnose_mutates_no_file_on_any_path(self):
        states = {
            "bare": None,
            "local": LOCAL_TOML,
            "legacy-gha": LEGACY_GHA_TOML,
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
    def test_legacy_gha_schema_is_surfaced_as_a_gap_never_local(self):
        # Legacy-GHA recognition was removed with the profile (ADR-0021; the two
        # legacy-schema installs were torn down). A leftover legacy section is an
        # unrecognized [merge-gate] like any other: a gap — but still NEVER
        # misread as a local gate with an absent hook (which would let #04
        # auto-install a local pre-push onto it).
        repo = _new_repo(LEGACY_GHA_TOML)
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertNotEqual(rec["state"], "legacy-schema/needs-migration")
        self.assertNotEqual(rec["applicability"], "parked")
        self.assertNotEqual(rec["enforcement"], "unwired")  # never installable-local
        self.assertTrue(harness_doctor._is_gap(rec))


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

    def test_github_actions_profile_is_surfaced_as_a_gap(self):
        # The github-actions profile was removed (ADR-0021) — 'local' is the only
        # profile. A leftover github-actions value is just another unrecognized
        # profile: surfaced as a gap, never blessed as parked.
        repo = _new_repo('[merge-gate]\nprofile = "github-actions"\n')
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertNotEqual(rec["applicability"], "parked")
        self.assertTrue(harness_doctor._is_gap(rec))


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
        # Coverage = |installed ∩ scaffold| ÷ |scaffold|, where
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


    def test_unrecognized_merge_gate_profile_stays_in_denominator_as_a_gap(self):
        # Panel blocker (b): a merge-gate the operator WANTED but whose
        # [merge-gate] resolves to an unrecognized profile (e.g. a leftover
        # github-actions value — removed, ADR-0021) must NOT vanish from the
        # denominator. Its intent is partial, so it stays an open coverage gap,
        # never silently covered (it must not vanish from the denominator).
        # agents-md + status-harness are genuinely present, so merge-gate is
        # the lone gap.
        repo = _new_repo('[merge-gate]\nprofile = "github-actions"\n')
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _install_status_harness(repo)
        records = harness_doctor.diagnose(repo)
        cov = harness_doctor.compute_coverage(records, {
            "scaffold": ["agents-md", "status-harness", "merge-gate"], "ci": False})
        self.assertIn("merge-gate", cov["applicable"])                # stays in denom
        self.assertNotIn("merge-gate", cov["covered"])                # open gap, not covered

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
        # Coverage is set-valued (|scaffold|). A hand-edited [harness]
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
        repo = _new_repo(LOCAL_TOML)
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


# --------------------------------------------------------------------------
# Phase-2 (scaffold-doctor #06): drift/stale on the enforcement axis, report-only
# (ADR-0030). Drift comes from the status-harness vendored byte-equality detector.
# --------------------------------------------------------------------------
def _install_and_drift(repo: Path, relpath) -> None:
    """Install the status-harness for real, then perturb ONE vendored file so it
    differs from its ~/.claude source — a `warn` (present-but-differs) action, the
    drift signal, never a `change` (missing file)."""
    _install_status_harness(repo)
    f = repo / relpath
    f.write_text(f.read_text(encoding="utf-8") + "\n# local dogfood edit\n",
                 encoding="utf-8")


class TestVendoredDrift(unittest.TestCase):
    def test_drifted_vendored_copy_is_state_drifted_not_partial(self):
        # AC1/AC2: a vendored status.py present-but-differing surfaces as a distinct
        # state="drifted" with intent=present (the file IS installed), NOT collapsed
        # into intent=partial (the #02 status quo that false-flags dogfood copies).
        repo = _new_repo()
        _install_and_drift(repo, Path("scripts") / "status.py")
        rec = _record(harness_doctor.diagnose(repo), "status-harness")
        self.assertEqual(rec["state"], "drifted")
        self.assertEqual(rec["intent"], "present")

    def test_drift_plus_missing_file_stays_a_real_gap_not_masked_as_drift(self):
        # AC2 discriminator (ADR-0030 §3): a concern with BOTH a drifted file (warn)
        # AND a genuinely missing file (change) is a real intent gap — drift must
        # never mask a half-install. Here status.py drifts but regen-status.yml is
        # removed → partial gap, state None (not "drifted").
        repo = _new_repo()
        _install_and_drift(repo, Path("scripts") / "status.py")
        (repo / ".github" / "workflows" / "regen-status.yml").unlink()
        rec = _record(harness_doctor.diagnose(repo), "status-harness")
        self.assertIsNone(rec["state"])           # NOT masked as drift
        self.assertEqual(rec["intent"], "partial")
        self.assertTrue(harness_doctor._is_gap(rec))

    def test_drifted_but_otherwise_conforming_repo_exits_0_no_false_flag(self):
        # AC3: drift is report-only. A repo whose ONLY deviation is a drifted
        # vendored copy (the dogfood-richer / #31-wiring case) must NOT false-flag
        # as a gap — it exits 0, while the drift stays visible in the record.
        repo = _new_repo(LOCAL_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _write_hook(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
        _install_and_drift(repo, Path("scripts") / "status.py")
        code, out, _err = _run_main([str(repo)])
        self.assertEqual(code, 0, msg=out)
        rec = _record(harness_doctor.diagnose(repo), "status-harness")
        self.assertEqual(rec["state"], "drifted")           # reported, not hidden
        self.assertFalse(harness_doctor._is_gap(rec))        # but never a gap

    def test_merge_gate_never_reports_drift(self):
        # AC4: drift scope is exactly {status.py, regen-status.yml, issue-tracker.md}
        # — all status-harness vendored files. The merge-gate vendors NO
        # byte-comparable file (git hooks + runtime .merge-gate/ output only), so it
        # can never be "drifted": the "validator snapshots" target is dropped.
        repo = _new_repo(LOCAL_TOML)
        _write_hook(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertNotEqual(rec["state"], "drifted")

    def test_agents_md_conflict_warn_stays_a_gap_never_drifted(self):
        # AC2 cross-detector guard (the load-bearing reason drift is status-harness-
        # specific): setup_status_harness emits `warn` for DRIFT (report-only), but
        # setup_agents_md emits `warn` for a State-4 CONFLICT (both files present,
        # CLAUDE.md lacks @AGENTS.md) — a REAL gap the operator must merge. agents-md
        # uses the default warn_is_drift=False, so its warn MUST stay intent=partial
        # / state=None / a gap, never be reclassified as report-only "drifted" (which
        # would exit 0, never auto-filled). A one-token flip of that default would
        # silently defeat this — so it is pinned here.
        repo = _new_repo()
        (repo / "AGENTS.md").write_text("# agents content\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("# claude content, no import\n", encoding="utf-8")
        rec = _record(harness_doctor.diagnose(repo), "agents-md")
        self.assertIsNone(rec["state"])                 # NOT "drifted"
        self.assertEqual(rec["intent"], "partial")
        self.assertTrue(harness_doctor._is_gap(rec))     # a real, fillable gap


def _write_hook_nonexec(repo: Path, name: str, body: str) -> None:
    """A marked hook git will NOT run — present + marked but lacking the
    user-executable bit. The enforcement-integrity 'broken' fixture (B1)."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    h = hooks / name
    h.write_text(body, encoding="utf-8")
    h.chmod(0o644)


class TestEnforcementIntegrity(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "exec-bit is meaningless off POSIX")
    def test_marked_but_non_executable_hook_is_broken_not_wired(self):
        # AC5 (B1, routed from #02): a pre-push carrying our marker but lacking the
        # user-executable bit — git will NOT run it — is reported `broken`, never
        # `wired`. The check is POSIX-guarded (skipped where the bit is meaningless).
        repo = _new_repo(LOCAL_TOML)
        _write_hook_nonexec(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\nexit 0\n")
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertEqual(rec["enforcement"], "broken")

    @unittest.skipUnless(os.name == "posix", "exec-bit is meaningless off POSIX")
    def test_broken_hook_is_a_gap_in_default_mode(self):
        # A broken (marked-but-non-executable) hook is a fillable gap locally — the
        # operator (or auto-fill re-running install → chmod) must fix it. A silently
        # inert gate must NOT read as conforming. Here agents-md + status-harness are
        # genuinely present, so the broken merge-gate hook is the lone gap → exit 2.
        repo = _new_repo(LOCAL_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _install_status_harness(repo)
        _write_hook_nonexec(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertTrue(harness_doctor._is_gap(rec))
        code, out, _err = _run_main([str(repo)])
        self.assertEqual(code, 2, msg=out)

    @unittest.skipUnless(os.name == "posix", "exec-bit is meaningless off POSIX")
    def test_operator_customized_hook_body_is_still_wired_not_stale(self):
        # AC6: hook-BODY / template-version staleness is OUT of scope. A pre-push
        # carrying the marker + an operator-customized prepended block (the #31
        # tombstone is the live instance), executable, is reported `wired` — the
        # body-diff is NOT flagged "stale" (it can't be honestly attested as such).
        repo = _new_repo(LOCAL_TOML)
        customized = ("# --- operator customization (kept) ---\n"
                      "#!/bin/sh\n# MERGE_GATE_WRAPPER\nexit 0\n")
        _write_hook(repo, "pre-push", customized)   # executable, marker + extra body
        rec = _record(harness_doctor.diagnose(repo), "merge-gate")
        self.assertEqual(rec["enforcement"], "wired")

    @unittest.skipUnless(os.name == "posix", "exec-bit is meaningless off POSIX")
    def test_enforcement_probe_stat_failure_degrades_not_crash(self):
        # diagnose() must NEVER raise (the read-only #02 B2 contract). hook_has_marker
        # swallows read errors, but the exec-bit probe's hook.stat() ran unguarded —
        # a stat race (file removed / permission flip between the marker read and the
        # stat) would escape diagnose(). Forced here: stat succeeds for the marker
        # probe's exists(), then raises on the exec probe → degrade to "wired"
        # (marker confirmed; the exec-check is best-effort), never a crash.
        repo = _new_repo(LOCAL_TOML)
        _write_hook(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
        real_stat = Path.stat
        seen = {"n": 0}
        def flaky(self, *a, **k):
            if self.name == "pre-push":
                seen["n"] += 1
                if seen["n"] >= 2:               # let exists() pass, fail the exec probe
                    raise OSError("simulated stat race")
            return real_stat(self, *a, **k)
        with mock.patch.object(Path, "stat", flaky):
            rec = _record(harness_doctor.diagnose(repo), "merge-gate")   # must not raise
        self.assertEqual(rec["enforcement"], "wired")


class TestCiMode(unittest.TestCase):
    def _intent_present_enforcement_unwired(self):
        """A repo with the full INTENT axis present but the merge-gate enforcement
        hook absent — the fresh-clone state (ADR-0020 Decision 2: legitimate)."""
        repo = _new_repo(LOCAL_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _install_status_harness(repo)
        return repo                                  # NO pre-push hook → unwired

    def test_ci_flag_excludes_enforcement_from_exit_code(self):
        # AC7 + AC9: a fresh clone (intent present, enforcement unwired) is a
        # fillable gap locally (exit 2 — auto-fill wants to wire it), but --ci
        # asserts the INTENT axis only and exits 0. Resolves the live _is_gap
        # contradiction (ADR-0030 §4 / ADR-0020 Decision 2).
        repo = self._intent_present_enforcement_unwired()
        self.assertEqual(_run_main([str(repo)])[0], 2)            # default: gap
        self.assertEqual(_run_main([str(repo), "--ci"])[0], 0)    # --ci: intent-only

    def test_ci_does_not_relax_a_real_intent_gap(self):
        # AC7 boundary: --ci relaxes ONLY the enforcement axis. A genuine INTENT
        # gap (a bare repo: agents-md/status-harness/merge-gate all absent) still
        # exits 2 under --ci — intent is exactly what CI asserts.
        self.assertEqual(_run_main([str(_new_repo()), "--ci"])[0], 2)

    def test_ci_reports_enforcement_records_labelled_out_of_scope(self):
        # AC8: --ci is honesty, not omission. Enforcement records are STILL reported
        # (table + JSON) — merely excluded from the exit code — and the report is
        # explicitly labelled intent-only / out-of-scope.
        repo = self._intent_present_enforcement_unwired()
        code, out, _err = _run_main([str(repo), "--ci", "--json"])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["intent_only"])
        mg = [c for c in payload["concerns"] if c["concern"] == "merge-gate"][0]
        self.assertEqual(mg["enforcement"], "unwired")       # reported, not omitted
        _c, hout, _e = _run_main([str(repo), "--ci"])
        self.assertIn("unwired", hout)                       # enforcement still in table
        self.assertIn("intent-only", hout.lower())           # labelled out-of-scope

    def test_no_ci_flag_omits_the_intent_only_label(self):
        # AC8 boundary: the label is --ci-only — a normal run carries no intent_only
        # key (the JSON shape is unchanged when the flag is absent).
        _c, out, _e = _run_main([str(_new_repo(LOCAL_TOML)), "--json"])
        self.assertNotIn("intent_only", json.loads(out))

    @unittest.skipUnless(os.name == "posix", "exec-bit is meaningless off POSIX")
    def test_ci_also_relaxes_a_broken_hook_not_just_unwired(self):
        # AC7 symmetry: --ci excludes the WHOLE enforcement axis. A `broken`
        # (marked-but-non-executable) hook is an enforcement gap by default (exit 2)
        # but is relaxed under --ci (exit 0), exactly like `unwired` — guarding a
        # regression that relaxed only one of the two enforcement-gap states.
        repo = _new_repo(LOCAL_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _install_status_harness(repo)
        _write_hook_nonexec(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")
        self.assertEqual(_run_main([str(repo)])[0], 2)            # default: broken is a gap
        self.assertEqual(_run_main([str(repo), "--ci"])[0], 0)    # --ci: relaxed


class TestPhase2ReadOnly(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "exec-bit is meaningless off POSIX")
    def test_phase2_paths_mutate_no_file_nor_hook_mode(self):
        # AC10: the new phase-2 read paths (drift detection, the exec-bit probe, and
        # --ci) preserve #02's zero-filesystem-mutation invariant — including the
        # hook's mode bits (the exec-bit probe reads st_mode; it must NEVER chmod).
        repo = _new_repo(LOCAL_TOML)
        (repo / "AGENTS.md").write_text("# proj\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        _install_and_drift(repo, Path("scripts") / "status.py")     # drifted copy
        _write_hook_nonexec(repo, "pre-push", "#!/bin/sh\n# MERGE_GATE_WRAPPER\n")  # broken
        hook = repo / ".git" / "hooks" / "pre-push"
        before, before_mode = _snapshot(repo), hook.stat().st_mode
        harness_doctor.diagnose(repo)
        _run_main([str(repo)])
        _run_main([str(repo), "--ci"])
        _run_main([str(repo), "--json"])
        self.assertEqual(_snapshot(repo), before)                    # no content write
        self.assertEqual(hook.stat().st_mode, before_mode)          # probe never chmods


if __name__ == "__main__":
    unittest.main()
