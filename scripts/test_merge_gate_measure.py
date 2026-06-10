#!/usr/bin/env python3
"""Regression suite for merge_gate_measure.py — local-profile measurement (#31).

Stdlib `unittest` only (pytest is not installed). Mirrors #30's test discipline
(design §7-1): grammar coverage for every #30 marker, unrecognized→raw+flag,
exit-passthrough (byte-identical stdout + rc), intervention-set, the summary
snapshot/row render, the ⓑ since-prev delta join, and ⓒ force⇒manual.

Run:  python3 scripts/test_merge_gate_measure.py -v
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import merge_gate_measure as mm  # noqa: E402
import merge_gate_local as mg  # noqa: E402


# --------------------------------------------------------------------------
# Exact #30 verify wording (pinned). These mirror merge_gate_local.cmd_verify;
# the coupling is the point — if #30 changes its print wording, this suite fails.
# --------------------------------------------------------------------------
def advisory(detail: str) -> str:
    return f"merge-gate advisory: {detail} — not blocking (advisory profile)"


REAL_LINES = {
    "fresh": "merge-gate: fresh passing review for 54eec8f752..d210322521 — PASS",
    "none": "merge-gate: no in-scope changes to gate — PASS",
    "missing": advisory("no review artefact for this base+diff"),
    "scope-mismatch": advisory("review scope changed since the artefact was produced"),
    "schema-incompatible": advisory("artefact schema is incompatible"),
    "tool-drift": advisory("reviewer/validator tool version changed (freshness_policy=tool-strict)"),
    "unreviewable": advisory("could not compute the review diff for the pushed range "
                             "(base/tip object missing or git error)"),
    "failing-incomplete": advisory("reviewer(s) failed: codex — review incomplete"),
    "failing-findings": advisory("review BLOCKED 2 finding(s)"),
    "no-base": "merge-gate advisory: could not resolve base ref (missing upstream/default); "
               "run `produce` or pass --base-sha (not blocking)",
    "bypassed": "merge-gate BYPASSED: shipping hotfix",
    "blocked": "merge-gate BLOCK: no review artefact for this base+diff.\n  Fix: ...",
}

# Expected (intervention, flagged) per state — the design §2ⓐ table.
EXPECTED = {
    "fresh": (False, False),
    "none": (False, False),
    "missing": (True, False),
    "scope-mismatch": (True, False),
    "unreviewable": (True, False),
    "failing-findings": (False, False),
    "failing-incomplete": (True, True),
    "schema-incompatible": (True, True),
    "tool-drift": (True, True),
    "no-base": (True, True),
    "bypassed": (False, True),
    "blocked": (False, True),
}


class TestGrammar(unittest.TestCase):
    def test_every_marker_classified(self):
        for state, line in REAL_LINES.items():
            vs = mm.parse_verify_state(line)
            self.assertEqual(vs.state, state, f"line {line!r} → {vs.state}, want {state}")
            interv, flagged = EXPECTED[state]
            self.assertEqual(vs.intervention, interv, f"{state} intervention")
            self.assertEqual(vs.flagged, flagged, f"{state} flagged")

    def test_advisory_block_not_confused_with_clientside_block(self):
        # advisory "review BLOCKED N" must NOT be read as a client-side block.
        self.assertEqual(mm.parse_verify_state(REAL_LINES["failing-findings"]).state,
                         "failing-findings")
        self.assertEqual(mm.parse_verify_state(REAL_LINES["blocked"]).state, "blocked")

    def test_unrecognized_is_raw_and_flagged(self):
        vs = mm.parse_verify_state("some unexpected output\nmerge-gate: surprise line")
        self.assertEqual(vs.state, "raw")
        self.assertTrue(vs.flagged)
        self.assertFalse(vs.intervention)
        self.assertEqual(vs.raw_line, "merge-gate: surprise line")

    def test_empty_output_is_raw(self):
        self.assertEqual(mm.parse_verify_state("").state, "raw")

    def test_decision_line_found_amid_noise(self):
        noisy = "warning: something\n" + REAL_LINES["missing"] + "\ntrailing\n"
        self.assertEqual(mm.parse_verify_state(noisy).state, "missing")


class TestInterventionSet(unittest.TestCase):
    def test_headline_set_is_the_clean_three(self):
        self.assertEqual(mm.HEADLINE_INTERVENTION_STATES,
                         frozenset({"missing", "scope-mismatch", "unreviewable"}))

    def test_headline_states_are_interventions(self):
        for s in mm.HEADLINE_INTERVENTION_STATES:
            self.assertTrue(mm.parse_verify_state(REAL_LINES[s]).intervention)

    def test_legit_block_and_pass_are_not_interventions(self):
        for s in ("fresh", "none", "failing-findings"):
            self.assertFalse(mm.parse_verify_state(REAL_LINES[s]).intervention)


class TestWaitTokenGrammar(unittest.TestCase):
    """#33 G1 — `cmd_verify` emits a stable stdout token when it waited for an
    in-flight produce; the wrapper PARSES it into verify_wait_seconds (never
    recomputes it wrapper-side, which would make the wrapper a second oracle).
    The token is an ANNOTATION, not a freshness state — the decision line still
    drives `state`."""

    WAIT = "merge-gate: waited 12s for in-flight produce"

    def test_wait_token_parsed_alongside_state(self):
        out = self.WAIT + "\n" + REAL_LINES["fresh"]
        vs = mm.parse_verify_state(out)
        self.assertEqual(vs.state, "fresh")     # the decision line still wins
        self.assertEqual(vs.wait_seconds, 12)

    def test_wait_token_absent_is_zero(self):
        self.assertEqual(mm.parse_verify_state(REAL_LINES["fresh"]).wait_seconds, 0)

    def test_wait_token_not_misread_as_a_state(self):
        # the wait line ALONE must not be classified as a freshness state.
        vs = mm.parse_verify_state(self.WAIT)
        self.assertEqual(vs.state, "raw")
        self.assertEqual(vs.wait_seconds, 12)
        self.assertNotIn(vs.state, mm.HEADLINE_INTERVENTION_STATES)

    def test_wait_then_block_keeps_block_state(self):
        # a wait that ends in a real block still records the wait + the block state.
        out = "merge-gate: waited 540s for in-flight produce\n" + REAL_LINES["failing-findings"]
        vs = mm.parse_verify_state(out)
        self.assertEqual(vs.state, "failing-findings")
        self.assertEqual(vs.wait_seconds, 540)


# --------------------------------------------------------------------------
# Row rendering — the worked example (design §3) maps cleanly to real summary.json.
# --------------------------------------------------------------------------
WORKED_SUMMARY = {
    "schema_version": 1,
    "verdict": "block",
    "block_count": 2,
    "reviewers": ["codex", "claude"],
    "codex_version": "codex-cli 0.134.0",
    "claude_model": "claude-opus-4-8[1m]",
    "enforcement_policy_at_produce": "advisory",
    "per_reviewer_timings": [
        {"reviewer": "codex", "reviewer_seconds": 75, "validator_seconds": 162},
        {"reviewer": "claude", "reviewer_seconds": 131, "validator_seconds": 175},
    ],
    "findings": [
        {"id": "codex:0", "producing_reviewers": ["codex"], "file": "merge_gate_local.py",
         "line_start": 1304, "severity": "high", "validator_verdict": "uphold",
         "block": True, "concordance_count": 2, "confidence_score": 6, "reviewer_confidence": 0.88},
        {"id": "claude:0", "producing_reviewers": ["claude"], "file": "merge_gate_local.py",
         "line_start": 1182, "severity": "medium", "validator_verdict": "uphold",
         "block": False, "concordance_count": 1, "confidence_score": 2, "reviewer_confidence": 0.45},
    ],
}


class TestRowRender(unittest.TestCase):
    def _row(self, **kw):
        from datetime import datetime, timezone
        defaults = dict(
            vs=mm.parse_verify_state(REAL_LINES["failing-findings"]),
            summary=WORKED_SUMMARY, base="54eec8f752aa", diff_hash="d210322521bb",
            tip_sha="abcdef0123456789", reviewers=["codex", "claude"],
            auto_since=0, intent=None,
            now=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc), canary=False)
        defaults.update(kw)
        return mm.build_row(**defaults)

    def test_header_and_machine_fields(self):
        row = self._row()
        self.assertIn("## push abcdef0123 · 2026-06-02T12:00:00Z · 54eec8f752..d210322521", row)
        self.assertIn("verdict(gate): block · block_count=2", row)
        self.assertIn("reviewers: codex, claude", row)
        self.assertIn("schema=1 enforcement=advisory", row)
        self.assertIn("codex_version=codex-cli 0.134.0", row)
        self.assertIn("claude_model=claude-opus-4-8[1m]", row)

    def test_latency_total_is_sum_of_per_reviewer(self):
        row = self._row()
        self.assertIn("latency: total=543s · codex r=75s v=162s · claude r=131s v=175s", row)

    def test_findings_rows_and_blank_human_columns(self):
        row = self._row()
        self.assertIn("| 0 | merge_gate_local.py:1304 | codex | high | uphold | ✓ | 2 | 6 | 0.88 | ⬜ | ⬜ |", row)
        self.assertIn("| 1 | merge_gate_local.py:1182 | claude | medium | uphold | ✗ | 1 | 2 | 0.45 | ⬜ | ⬜ |", row)
        self.assertIn("human push-verdict: —", row)

    def test_no_summary_degrades_to_dashes(self):
        row = self._row(summary=None)
        self.assertIn("verdict(gate): — · block_count=—", row)
        self.assertIn("- latency: —", row)
        # header still present (the table exists even with no findings)
        self.assertIn("| # | finding (file:line) |", row)

    def test_canary_marked_and_excluded_note(self):
        row = self._row(canary=True)
        self.assertIn("· (canary) ·", row)
        self.assertIn("(canary — not adjudicated)", row)

    def test_intent_and_auto_context_rendered(self):
        row = self._row(auto_since=3, intent="force")
        self.assertIn("auto-produces-since-prev ⓑ=3 · intent/force ⓒ=force", row)

    def test_raw_state_flags_the_unrecognized_line(self):
        vs = mm.parse_verify_state("totally unexpected gate output")
        row = self._row(vs=vs, summary=None)
        self.assertIn("freshness ⓐ: raw", row)
        self.assertIn("[flagged: totally unexpected gate output]", row)

    def test_verify_wait_seconds_rendered_when_waited(self):
        # #33 G1 — a waited-then-fresh push records the wait on the freshness line.
        vs = mm.parse_verify_state(
            "merge-gate: waited 12s for in-flight produce\n" + REAL_LINES["fresh"])
        row = self._row(vs=vs, summary=None)
        self.assertIn("freshness ⓐ: fresh", row)
        self.assertIn("wait=12s", row)

    def test_no_wait_omits_the_wait_field(self):
        vs = mm.parse_verify_state(REAL_LINES["fresh"])
        row = self._row(vs=vs, summary=None)
        self.assertNotIn("wait=", row)  # existing rows/tests stay unchanged


class TestAdvisoryConformanceLine(unittest.TestCase):
    """#35 (A) — build_row surfaces a reviewer that produced NO usable signal
    (codex_status != ok) so soft-steer non-conformance is not silently read as
    'the reviewer agreed'; an all-ok row stays byte-unchanged (no advisory line)."""

    def _summary(self, statuses):
        s = dict(WORKED_SUMMARY)
        s["per_reviewer_timings"] = [
            {"reviewer": r, "codex_status": st, "reviewer_seconds": 1, "validator_seconds": 1}
            for r, st in statuses]
        return s

    def _row(self, summary):
        from datetime import datetime, timezone
        return mm.build_row(
            vs=mm.parse_verify_state(REAL_LINES["failing-findings"]),
            summary=summary, base="b", diff_hash="d", tip_sha="t",
            reviewers=["codex", "claude"], auto_since=0, intent=None,
            now=datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc), canary=False)

    def test_emitted_when_reviewer_malformed(self):
        row = self._row(self._summary([("codex", "ok"), ("claude", "malformed-payload")]))
        self.assertIn("- advisory conformance ⓓ: claude=malformed-payload", row)
        self.assertIn("no usable signal — soft-steer #35", row)

    def test_omitted_when_all_ok(self):
        row = self._row(self._summary([("codex", "ok"), ("claude", "ok")]))
        self.assertNotIn("advisory conformance", row)

    def test_missing_status_treated_as_ok(self):
        # Older/partial summaries whose per_reviewer_timings carry no codex_status
        # must NOT trigger the line (the WORKED_SUMMARY fixture has none).
        row = self._row(dict(WORKED_SUMMARY))
        self.assertNotIn("advisory conformance", row)

    def test_helper_none_for_no_summary(self):
        self.assertIsNone(mm._advisory_conformance_line(None))


# --------------------------------------------------------------------------
# ⓑ — scheduler state-dir reader + since-prev delta.
# --------------------------------------------------------------------------
class TestSchedulerReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.state_root = Path(self.tmp.name) / "state"
        self.measure = Path(self.tmp.name) / "measure"
        self._env = {
            "MERGE_GATE_STATE_ROOT": str(self.state_root),
            "MERGE_GATE_MEASURE_DIR": str(self.measure),
        }
        self._saved = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)
        # Scheduler's own state dir = STATE_ROOT / sha1(root)[:16].
        self.sdir = self.state_root / hashlib.sha1(str(self.root).encode()).hexdigest()[:16]
        self.sdir.mkdir(parents=True)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _log(self, n):
        (self.sdir / "produce.log").write_text(
            "".join(f"merge-gate produce: verdict=pass block_count=0 (1 reviewer(s))\n"
                    for _ in range(n)))
        (self.sdir / "state.json").write_text(json.dumps({"last_produce_ts": 123.0}))

    def test_reads_count_and_ts(self):
        self._log(3)
        stats = mm.read_scheduler_stats(self.root)
        self.assertEqual(stats["produce_count"], 3)
        self.assertEqual(stats["last_produce_ts"], 123.0)

    def test_missing_state_dir_is_zero(self):
        stats = mm.read_scheduler_stats(Path(self.tmp.name) / "nonexistent")
        self.assertEqual(stats["produce_count"], 0)
        self.assertEqual(stats["last_produce_ts"], 0.0)

    def test_since_prev_delta_committed_on_cursor(self):
        # Reading the delta does NOT advance the sidecar; only committing the
        # cursor does (F1 consume-before-commit).
        self._log(2)
        delta, cursor = mm.read_auto_produce_delta(self.root)
        self.assertEqual(delta, 2)  # prev 0 → 2
        # uncommitted: re-reading still sees the full delta
        self.assertEqual(mm.read_auto_produce_delta(self.root)[0], 2)
        mm.commit_auto_produce_cursor(self.root, cursor)
        self.assertEqual(mm.read_auto_produce_delta(self.root)[0], 0)  # caught up
        self._log(5)
        delta2, cursor2 = mm.read_auto_produce_delta(self.root)
        self.assertEqual(delta2, 3)  # 5 - 2
        mm.commit_auto_produce_cursor(self.root, cursor2)
        self.assertEqual(mm.read_auto_produce_delta(self.root)[0], 0)

    def test_failed_append_preserves_auto_delta(self):
        # F1 regression: if the ledger append raises, cmd_verify_wrap must NOT
        # advance the ⓑ cursor, so the auto-produce delta stays attributable to
        # the next row (never silently swallowed) — and the push still exits 0.
        self._log(2)
        orig_append, orig_run = mm.append_row, mm.run_real
        # Use a state that DOES append ("missing"); "none" now early-returns before
        # the append, so it would not reach the boom and would not test F1.
        mm.run_real = lambda argv: (0, (REAL_LINES["missing"] + "\n").encode(), b"")

        def boom(path, section):
            raise OSError("disk full")

        mm.append_row = boom
        try:
            ns = mm.build_parser().parse_args(
                ["--cwd", str(self.root), "verify", "--base-sha", "0" * 40, "--tip-sha", "x"])
            rc = mm.cmd_verify_wrap(
                ["verify", "--base-sha", "0" * 40, "--tip-sha", "x"], ns)
            self.assertEqual(rc, 0)  # behaviour-neutral: push still succeeds
        finally:
            mm.append_row, mm.run_real = orig_append, orig_run
        # cursor never committed → delta still attributable on the next read
        self.assertEqual(mm.read_auto_produce_delta(self.root)[0], 2)


# --------------------------------------------------------------------------
# ⓒ — produce-intent capture (force ⇒ manual) + join.
# --------------------------------------------------------------------------
class TestProduceIntent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.measure = self.root / "measure"
        self._saved = os.environ.get("MERGE_GATE_MEASURE_DIR")
        os.environ["MERGE_GATE_MEASURE_DIR"] = str(self.measure)
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("MERGE_GATE_MEASURE_DIR", None)
        else:
            os.environ["MERGE_GATE_MEASURE_DIR"] = self._saved

    def test_force_joins_as_manual(self):
        mm.record_produce_intent(self.root, "deadbeef", force=True, intent=None, ts=1.0)
        self.assertEqual(mm.lookup_intent(self.root, "deadbeef"), "force")

    def test_intent_takes_precedence_over_force(self):
        mm.record_produce_intent(self.root, "cafef00d", force=True, intent="ship the fix", ts=2.0)
        self.assertEqual(mm.lookup_intent(self.root, "cafef00d"), "intent")

    def test_no_force_no_intent_is_none(self):
        mm.record_produce_intent(self.root, "0000", force=False, intent=None, ts=3.0)
        self.assertIsNone(mm.lookup_intent(self.root, "0000"))

    def test_lookup_unknown_diff_is_none(self):
        self.assertIsNone(mm.lookup_intent(self.root, "nope"))
        self.assertIsNone(mm.lookup_intent(self.root, None))

    def test_most_recent_entry_wins(self):
        mm.record_produce_intent(self.root, "h", force=True, intent=None, ts=1.0)
        mm.record_produce_intent(self.root, "h", force=False, intent="later", ts=2.0)
        self.assertEqual(mm.lookup_intent(self.root, "h"), "intent")


# --------------------------------------------------------------------------
# Integration: locate_summary recompute + snapshot (real git repo, real mg).
# --------------------------------------------------------------------------
def _git(cwd, *args, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run(["git", *args], cwd=str(cwd), env=e,
                          capture_output=True, text=True, check=True)


class TestLocateSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        _git(self.root, "init", "-q", "-b", "main", env=self.env)
        (self.root / "a.py").write_text("base\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "base", env=self.env)
        self.base = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()
        (self.root / "a.py").write_text("base\nchange\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "tip", env=self.env)
        self.tip = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()

    def test_recompute_finds_the_snapshot(self):
        cfg = mg.load_config(self.root)
        cd = mg.canonical_diff_at_commit(self.root, self.base, self.tip,
                                         cfg.review_globs, cfg.ignore_globs)
        tdir = mg.tuple_dir(self.root / cfg.artifact_root, self.base, cd["diff_hash"])
        tdir.mkdir(parents=True)
        (tdir / "summary.json").write_text(json.dumps({"verdict": "pass", "block_count": 0,
                                                        "diff_hash": cd["diff_hash"]}))
        base, diff_hash, summary = mm.locate_summary(self.root, None, self.base, self.tip)
        self.assertEqual(base, self.base)
        self.assertEqual(diff_hash, cd["diff_hash"])
        self.assertIsNotNone(summary)
        self.assertEqual(summary["verdict"], "pass")

    def test_missing_summary_degrades_cleanly(self):
        # No artefact written → (base, diff_hash, None), never raises.
        base, diff_hash, summary = mm.locate_summary(self.root, None, self.base, self.tip)
        self.assertEqual(base, self.base)
        self.assertIsNotNone(diff_hash)
        self.assertIsNone(summary)


# --------------------------------------------------------------------------
# Citation snapshot (design §8 citation-preservation) — the validator citation
# lives ONLY in the transient per-reviewer validators.json lines[]; the row must
# durably snapshot it so dismiss-accuracy stays auditable after GC.
# --------------------------------------------------------------------------
# Real line grammar:  [SEV] verdict id=<fid> <file>:<line> — <citation text>
# (separator before the citation is space, U+2014 EM DASH, space).
CLAUDE_LINES = [
    "[HIGH] uphold id=claude:finding-0 scripts/merge_gate_local.py:1304 — "
    "guard cited only covers merge_gate_scheduler.py; status.py has no check",
    "[MEDIUM] dismiss id=claude:finding-1 scripts/merge_gate_local.py:1182 — "
    "startswith(\"CLAUDE_CODE_\") strips CLAUDE_CODE_OAUTH_TOKEN — but that key is unset here",
    "this is not a validator line and must be skipped",
]
CODEX_LINES = [
    "[HIGH] uphold id=codex:finding-0 scripts/merge_gate_local.py:1304 — "
    "\"Hook inheritance is harmless here\" but session_devlog.py carries no guard",
    "[LOW] unsure id=codex:finding-1 scripts/merge_gate_local.py:1306 — "
    "subprocess.run has no timeout= whereas reviewer path uses 600s",
]


def _write_validators(subdir: Path, name: str, lines: list[str]) -> None:
    subdir.mkdir(parents=True, exist_ok=True)
    (subdir / "validators.json").write_text(json.dumps(
        {"validators": [{"name": name, "lines": lines}], "aggregate": []}))


class TestReadCitations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tdir = Path(self.tmp.name) / "tuple"
        _write_validators(self.tdir / "claude", "claude", CLAUDE_LINES)
        _write_validators(self.tdir / "codex", "codex", CODEX_LINES)

    def test_maps_fid_to_citation_across_subdirs(self):
        cits = mm.read_citations(self.tdir)
        # uphold / dismiss / unsure all captured, across both reviewer subdirs.
        self.assertEqual(cits["claude:finding-0"],
                         "guard cited only covers merge_gate_scheduler.py; status.py has no check")
        self.assertEqual(cits["claude:finding-1"],
                         "startswith(\"CLAUDE_CODE_\") strips CLAUDE_CODE_OAUTH_TOKEN — but that key is unset here")
        self.assertEqual(cits["codex:finding-0"],
                         "\"Hook inheritance is harmless here\" but session_devlog.py carries no guard")
        self.assertEqual(cits["codex:finding-1"],
                         "subprocess.run has no timeout= whereas reviewer path uses 600s")

    def test_non_matching_lines_skipped(self):
        cits = mm.read_citations(self.tdir)
        # the "this is not a validator line" line yields no entry.
        self.assertEqual(len(cits), 4)

    def test_missing_dir_returns_empty_without_raising(self):
        self.assertEqual(mm.read_citations(Path(self.tmp.name) / "nope"), {})

    def test_bad_json_returns_what_it_has(self):
        bad = Path(self.tmp.name) / "tuple2"
        (bad / "claude").mkdir(parents=True)
        (bad / "claude" / "validators.json").write_text("{ not json")
        _write_validators(bad / "codex", "codex", CODEX_LINES)
        cits = mm.read_citations(bad)
        # claude subdir unreadable → only codex citations survive, no raise.
        self.assertEqual(set(cits), {"codex:finding-0", "codex:finding-1"})


class TestCitationDurability(unittest.TestCase):
    """THE LOAD-BEARING TEST: the citation must survive into the built row even
    after the source validators.{json,md} are GC-deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        _git(self.root, "init", "-q", "-b", "main", env=self.env)
        (self.root / "a.py").write_text("base\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "base", env=self.env)
        self.base = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()
        (self.root / "a.py").write_text("base\nchange\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "tip", env=self.env)
        self.tip = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()

    def test_citation_survives_validators_gc(self):
        from datetime import datetime, timezone
        cfg = mg.load_config(self.root)
        cd = mg.canonical_diff_at_commit(self.root, self.base, self.tip,
                                         cfg.review_globs, cfg.ignore_globs)
        tdir = mg.tuple_dir(self.root / cfg.artifact_root, self.base, cd["diff_hash"])
        tdir.mkdir(parents=True)
        summary = {
            "verdict": "block", "block_count": 1, "reviewers": ["codex", "claude"],
            "findings": [
                {"id": "codex:finding-0", "producing_reviewers": ["codex"],
                 "file": "scripts/merge_gate_local.py", "line_start": 1304,
                 "severity": "high", "validator_verdict": "uphold", "block": True,
                 "concordance_count": 1, "confidence_score": 6, "reviewer_confidence": 0.9},
                {"id": "claude:finding-1", "producing_reviewers": ["claude"],
                 "file": "scripts/merge_gate_local.py", "line_start": 1182,
                 "severity": "medium", "validator_verdict": "dismiss", "block": False,
                 "concordance_count": 1, "confidence_score": 2, "reviewer_confidence": 0.4},
            ],
        }
        (tdir / "summary.json").write_text(json.dumps(summary))
        _write_validators(tdir / "claude", "claude", CLAUDE_LINES)
        _write_validators(tdir / "codex", "codex", CODEX_LINES)

        # Snapshot the citations BEFORE building the row, then build the row.
        cits = mm.locate_citations(self.root, self.base, cd["diff_hash"])
        self.assertIn("codex:finding-0", cits)
        row = mm.build_row(
            mm.parse_verify_state(REAL_LINES["failing-findings"]), summary,
            self.base, cd["diff_hash"], self.tip, ["codex", "claude"],
            0, None, datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc), False,
            citations=cits)

        # GC the transient sources — simulate the artefact-root sweep.
        (tdir / "claude" / "validators.json").unlink()
        (tdir / "codex" / "validators.json").unlink()
        for stem in ("claude", "codex"):
            md = tdir / stem / "validators.md"
            if md.exists():
                md.unlink()

        # The dismiss citation text must still be present in the already-built row.
        self.assertIn(
            "startswith(\"CLAUDE_CODE_\") strips CLAUDE_CODE_OAUTH_TOKEN — but that key is unset here",
            row)
        self.assertIn(
            "\"Hook inheritance is harmless here\" but session_devlog.py carries no guard",
            row)
        # keyed by finding index AND fid so it joins back unambiguously.
        self.assertIn("[0] codex:finding-0 · uphold ·", row)
        self.assertIn("[1] claude:finding-1 · dismiss ·", row)

    def test_locate_citations_degrades_without_raising(self):
        # No tuple dir / no validators.json → {} (best-effort, behaviour-neutral).
        cd = mg.canonical_diff_at_commit(
            self.root, self.base, self.tip,
            mg.load_config(self.root).review_globs,
            mg.load_config(self.root).ignore_globs)
        self.assertEqual(mm.locate_citations(self.root, self.base, cd["diff_hash"]), {})
        self.assertEqual(mm.locate_citations(self.root, None, None), {})
        self.assertEqual(mm.locate_citations(self.root, self.base, ""), {})


class TestCitationSubBlockRender(unittest.TestCase):
    def _row(self, citations):
        from datetime import datetime, timezone
        return mm.build_row(
            vs=mm.parse_verify_state(REAL_LINES["failing-findings"]),
            summary=WORKED_SUMMARY, base="54eec8f752aa", diff_hash="d210322521bb",
            tip_sha="abcdef0123456789", reviewers=["codex", "claude"],
            auto_since=0, intent=None,
            now=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc), canary=False,
            citations=citations)

    def test_sub_block_keyed_by_index_and_fid(self):
        cits = {"codex:0": "the codex citation text",
                "claude:0": "the claude citation text"}
        row = self._row(cits)
        self.assertIn("validator citations", row)
        self.assertIn("[0] codex:0 · uphold · the codex citation text", row)
        self.assertIn("[1] claude:0 · uphold · the claude citation text", row)

    def test_empty_citations_emit_no_sub_block(self):
        # When citations is empty/None, NO sub-block — and the prior row-render
        # assertions still hold (existing rows/tests unaffected).
        for cits in ({}, None):
            row = self._row(cits)
            self.assertNotIn("validator citations", row)
            self.assertIn("| 0 | merge_gate_local.py:1304 | codex | high | uphold | ✓ | 2 | 6 | 0.88 | ⬜ | ⬜ |", row)
            self.assertIn("human push-verdict: —", row)

    def test_finding_without_citation_omitted_from_sub_block(self):
        # Only findings WITH a citation appear in the sub-block.
        row = self._row({"codex:0": "only codex has one"})
        self.assertIn("[0] codex:0 · uphold · only codex has one", row)
        self.assertNotIn("[1] claude:0", row)


# --------------------------------------------------------------------------
# Integration: ⓒ join e2e + ⓑ canary detection on a real git repo (real mg).
# --------------------------------------------------------------------------
class TestRealRepoSeams(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.measure = self.root / "measure"
        self._saved = os.environ.get("MERGE_GATE_MEASURE_DIR")
        os.environ["MERGE_GATE_MEASURE_DIR"] = str(self.measure)
        self.addCleanup(self._restore)
        self.env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        _git(self.root, "init", "-q", "-b", "main", env=self.env)
        (self.root / "a.py").write_text("base\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "base", env=self.env)
        self.base = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()
        (self.root / "a.py").write_text("base\nchange\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "tip", env=self.env)
        self.tip = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()

    def _restore(self):
        if self._saved is None:
            os.environ.pop("MERGE_GATE_MEASURE_DIR", None)
        else:
            os.environ["MERGE_GATE_MEASURE_DIR"] = self._saved

    def test_produce_intent_joins_onto_verify_diff_hash(self):
        # ⓒ records keyed by produce's diff_hash; ⓐ locates by verify's diff_hash.
        # On a clean tree (working tree == tip) the two recomputations agree (D4),
        # so the manual-force intent recorded by ⓒ joins onto ⓐ's row.
        produce_hash = mm._compute_diff_hash(self.root, self.base)
        self.assertIsNotNone(produce_hash)
        _, verify_hash, _ = mm.locate_summary(self.root, self.base, None, self.tip)
        self.assertEqual(produce_hash, verify_hash)  # the join condition
        mm.record_produce_intent(self.root, produce_hash, force=True, intent=None, ts=1.0)
        self.assertEqual(mm.lookup_intent(self.root, verify_hash), "force")

    def test_canary_trailer_detected_and_plain_tip_is_not(self):
        # §4: a tip carrying a `Merge-Gate-Canary:` trailer is excluded from N.
        self.assertFalse(mm._is_canary(self.root, self.tip))  # plain commit
        _git(self.root, "commit", "-q", "--allow-empty",
             "-m", "calibration push", "-m", "Merge-Gate-Canary: calibration",
             env=self.env)
        canary_tip = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()
        self.assertTrue(mm._is_canary(self.root, canary_tip))
        self.assertFalse(mm._is_canary(self.root, None))  # no tip → not canary


# --------------------------------------------------------------------------
# Integration: exit-passthrough + tee + row append (fake real wrapper).
# --------------------------------------------------------------------------
FAKE_REAL_TEMPLATE = """\
import sys
sys.stdout.write({stdout!r})
sys.stderr.write({stderr!r})
sys.exit({code})
"""


class TestExitPassthrough(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.measure = self.root / "measure"
        self.state_root = self.root / "state"

    def _run(self, stdout, stderr, code):
        fake = self.root / "fake_real.py"
        fake.write_text(FAKE_REAL_TEMPLATE.format(stdout=stdout, stderr=stderr, code=code))
        env = {**os.environ,
               "MERGE_GATE_MEASURE_REAL": str(fake),
               "MERGE_GATE_MEASURE_DIR": str(self.measure),
               "MERGE_GATE_STATE_ROOT": str(self.state_root)}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "merge_gate_measure.py"),
             "--cwd", str(self.root), "verify",
             "--base-sha", "0" * 40, "--tip-sha", "deadbeef"],
            capture_output=True, env=env)

    def test_stdout_byte_identical_and_exit_code_passthrough(self):
        out = REAL_LINES["missing"] + "\n"
        p = self._run(out, "stderr noise\n", 0)
        self.assertEqual(p.stdout.decode(), out)  # byte-for-byte
        self.assertEqual(p.stderr.decode(), "stderr noise\n")
        self.assertEqual(p.returncode, 0)

    def test_large_interleaved_output_streamed_byte_identical(self):
        # #33 G4 — the wrapper must STREAM both pipes (not buffer-until-exit), so a
        # multi-minute wait shows progress live. The 2-pipe pump must also not
        # deadlock on output larger than a pipe buffer (~64KB). Prove both streams
        # are captured byte-for-byte at sizes that would deadlock a single drain.
        big_out = ("o" * 100 + "\n") * 4000 + REAL_LINES["missing"] + "\n"
        big_err = ("e" * 100 + "\n") * 4000
        p = self._run(big_out, big_err, 0)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.decode(), big_out)   # byte-for-byte, no truncation
        self.assertEqual(p.stderr.decode(), big_err)
        # and the row still parsed from the (streamed) decision line
        self.assertIn("freshness ⓐ: missing", (self.measure / "log.md").read_text(encoding="utf-8"))

    def test_blocking_exit_code_preserved(self):
        # under a later client-side-blocking flip, a non-zero block must pass through.
        p = self._run(REAL_LINES["blocked"] + "\n", "", 1)
        self.assertEqual(p.returncode, 1)

    def test_row_appended_with_parsed_state(self):
        self._run(REAL_LINES["missing"] + "\n", "", 0)
        ledger = (self.measure / "log.md").read_text(encoding="utf-8")
        self.assertIn("## push deadbeef", ledger)
        self.assertIn("freshness ⓐ: missing   intervention: yes", ledger)

    def test_no_in_scope_change_skips_row(self):
        # A push with no in-scope changes (state "none") is not a measurement
        # event — the wrapper must NOT append a row. This breaks the ledger-push
        # recursion: committing+pushing a ledger-only change yields an empty
        # in-scope diff (state "none"), so it spawns no new row and a clean tree
        # becomes reachable. push-N then counts only pushes the gate evaluated.
        # Still behaviour-neutral: stdout + exit code pass through unchanged.
        out = REAL_LINES["none"] + "\n"
        p = self._run(out, "", 0)
        self.assertEqual(p.stdout.decode(), out)   # byte-identical passthrough
        self.assertEqual(p.returncode, 0)
        self.assertFalse((self.measure / "log.md").exists(),
                         "a no-in-scope push must not append a ledger row")

    def test_capture_failure_never_breaks_the_push(self):
        # Point the ledger dir at an unwritable path → row append fails, but the
        # exit code + stdout still pass through (behaviour-neutral). Uses a state
        # that DOES append ("missing"); "none" would now skip the append entirely
        # (test_no_in_scope_change_skips_row), so it would not exercise this path.
        fake = self.root / "fake_real.py"
        fake.write_text(FAKE_REAL_TEMPLATE.format(stdout=REAL_LINES["missing"] + "\n",
                                                  stderr="", code=0))
        env = {**os.environ,
               "MERGE_GATE_MEASURE_REAL": str(fake),
               "MERGE_GATE_MEASURE_DIR": "/proc/nonexistent/cannot/write",
               "MERGE_GATE_STATE_ROOT": str(self.state_root)}
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "merge_gate_measure.py"),
             "--cwd", str(self.root), "verify", "--base-sha", "0" * 40, "--tip-sha", "x"],
            capture_output=True, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn("no review artefact", p.stdout.decode())


class TestProduceShimSubprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.measure = self.root / "measure"

    def test_force_alias_records_intent_and_passes_through(self):
        fake = self.root / "fake_real.py"
        fake.write_text(FAKE_REAL_TEMPLATE.format(
            stdout="merge-gate produce: verdict=pass block_count=0 (1 reviewer(s))\n",
            stderr="", code=0))
        env = {**os.environ,
               "MERGE_GATE_MEASURE_REAL": str(fake),
               "MERGE_GATE_MEASURE_DIR": str(self.measure)}
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "merge_gate_measure.py"),
             "--cwd", str(self.root), "force"],
            capture_output=True, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn("verdict=pass", p.stdout.decode())
        recs = [json.loads(ln) for ln in
                (self.measure / "produce-intents.jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["force"])  # force ⇒ manual by construction


class TestLedgerOnlyPushSkipsRowEndToEnd(unittest.TestCase):
    """The recursion fix, end-to-end (real git + real merge_gate_local): when the
    measurement dir is in `ignore_globs`, a push touching ONLY that dir has an empty
    in-scope diff → verify reports `no in-scope changes to gate` (state none) → the
    wrapper writes NO row, so the ledger can be committed+pushed without spawning a
    row. A code-touching push still records a row. (review_globs=`**/*` otherwise
    reviews the ledger itself, so without the ignore this would be state `missing`.)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        _git(self.root, "init", "-q", "-b", "main", env=self.env)
        (self.root / "harness.toml").write_text(
            '[merge-gate]\nprofile = "local"\n\n[merge-gate.local]\n'
            'review_globs = ["**/*"]\n'
            'ignore_globs = [".scratch/merge-gate-measurement/**"]\n')
        (self.root / "a.py").write_text("base\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "base", env=self.env)
        self.base = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()

    def _run(self, base, tip):
        env = {k: v for k, v in os.environ.items()
               if k not in ("MERGE_GATE_MEASURE_REAL", "MERGE_GATE_MEASURE_DIR",
                            "MERGE_GATE_STATE_ROOT")}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "merge_gate_measure.py"),
             "--cwd", str(self.root), "verify", "--base-sha", base, "--tip-sha", tip],
            capture_output=True, env=env, text=True)

    def _ledger(self):
        p = self.root / ".scratch" / "merge-gate-measurement" / "log.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_ledger_only_push_writes_no_row(self):
        led = self.root / ".scratch" / "merge-gate-measurement"
        led.mkdir(parents=True)
        (led / "log.md").write_text("# ledger\n")  # no "## push" section yet
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "ledger only", env=self.env)
        tip = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()
        p = self._run(self.base, tip)
        self.assertEqual(p.returncode, 0)
        self.assertIn("no in-scope changes to gate", p.stdout)  # gate saw nothing in scope
        self.assertNotIn("## push", self._ledger())             # → wrapper appended no row

    def test_code_push_still_writes_a_row(self):
        (self.root / "a.py").write_text("base\nmore\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "code", env=self.env)
        tip = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()
        p = self._run(self.base, tip)
        self.assertEqual(p.returncode, 0)
        self.assertIn("## push", self._ledger())                # code change → row recorded


class TestFreshWithFindingsRowEndToEnd(unittest.TestCase):
    """#33 4f-(a) done-bar, through the WRAPPER: a produced artefact (verdict pass
    with a non-blocking finding) → real `verify` reports `fresh` → the wrapper
    snapshots a `fresh` row WITH the findings table (not `missing`)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        _git(self.root, "init", "-q", "-b", "main", env=self.env)
        (self.root / "harness.toml").write_text(
            '[merge-gate]\nprofile = "local"\n\n[merge-gate.local]\n'
            'review_globs = ["**/*"]\n'
            'ignore_globs = [".codex-review/**", ".scratch/merge-gate-measurement/**"]\n')
        (self.root / "a.py").write_text("base\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "base", env=self.env)
        self.base = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()
        (self.root / "a.py").write_text("base\nfeature\n")
        _git(self.root, "add", "-A", env=self.env)
        _git(self.root, "commit", "-q", "-m", "feature", env=self.env)
        self.tip = _git(self.root, "rev-parse", "HEAD", env=self.env).stdout.strip()

    def _seed_fresh_artefact(self):
        cfg = mg.load_config(self.root)
        scope = mg.review_scope_hash(cfg)
        cd = mg.canonical_diff_at_commit(self.root, self.base, self.tip,
                                         cfg.review_globs, cfg.ignore_globs)
        summary = {
            "schema_version": 1, "base_sha": self.base, "diff_hash": cd["diff_hash"],
            "review_scope_hash": scope, "verdict": "pass", "block_count": 0,
            "reviewers": ["codex"], "codex_version": "codex-cli 0.0.0",
            "claude_model": None, "enforcement_policy_at_produce": "advisory",
            "per_reviewer_timings": [{"reviewer": "codex", "codex_status": "ok",
                                      "reviewer_seconds": 10, "validator_seconds": 20}],
            "findings": [{"id": "codex:0", "producing_reviewers": ["codex"],
                          "file": "a.py", "line_start": 2, "severity": "medium",
                          "validator_verdict": "uphold", "block": False,
                          "concordance_count": 1, "confidence_score": 2,
                          "reviewer_confidence": 0.5}],
        }
        mg.write_summary_atomic(mg.tuple_dir(self.root / cfg.artifact_root,
                                             self.base, cd["diff_hash"]), summary)

    def test_fresh_with_findings_row(self):
        self._seed_fresh_artefact()
        env = {k: v for k, v in os.environ.items()
               if k not in ("MERGE_GATE_MEASURE_REAL", "MERGE_GATE_MEASURE_DIR",
                            "MERGE_GATE_STATE_ROOT")}
        p = subprocess.run(
            [sys.executable, str(SCRIPTS / "merge_gate_measure.py"),
             "--cwd", str(self.root), "verify",
             "--base-sha", self.base, "--tip-sha", self.tip],
            capture_output=True, env=env, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("fresh passing review", p.stdout)             # real verify → fresh
        ledger = (self.root / ".scratch" / "merge-gate-measurement" / "log.md").read_text()
        self.assertIn("freshness ⓐ: fresh", ledger)                 # row is fresh
        self.assertIn("intervention: no", ledger)                   # not an intervention
        self.assertIn("a.py:2", ledger)                             # WITH the finding


# --------------------------------------------------------------------------
# Push-time adjudication reminder — when a push CAPTURES findings, nudge the
# operator (in-session, via the wrapper's stderr) to run blind adjudication in
# a terminal. Adjudication itself is never automated (design §3 — the human
# answers real?/should-block? blind); this only surfaces that work is pending.
# --------------------------------------------------------------------------
class _Cap:
    """Stand-in for sys.stdout/stderr: `.buffer` absorbs _tee's byte writes,
    `.write` captures the reminder's text writes."""
    def __init__(self):
        self.buffer = io.BytesIO()
        self._s = io.StringIO()

    def write(self, s):
        self._s.write(s)

    def flush(self):
        pass

    @property
    def value(self):
        return self._s.getvalue()


class TestAdjudicationReminder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self._env = {
            "MERGE_GATE_STATE_ROOT": str(Path(self.tmp.name) / "state"),
            "MERGE_GATE_MEASURE_DIR": str(Path(self.tmp.name) / "measure"),
        }
        self._saved_env = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self._saved_fn = {k: getattr(mm, k)
                          for k in ("run_real", "locate_summary", "_is_canary")}
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._saved_fn.items():
            setattr(mm, k, v)

    def _drive(self, summary, state="fresh", canary=False):
        """Push through cmd_verify_wrap with a fake verify + summary, returning
        (rc, captured-stderr). Writes only to the temp measure dir."""
        mm.run_real = lambda argv: (0, (REAL_LINES[state] + "\n").encode(), b"")
        mm.locate_summary = lambda *a, **k: ("base000000", "diff000000", summary)
        mm._is_canary = lambda root, tip: canary
        argv = ["verify", "--base-sha", "0" * 40, "--tip-sha", "x"]
        ns = mm.build_parser().parse_args(["--cwd", str(self.root)] + argv)
        out, err = _Cap(), _Cap()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = mm.cmd_verify_wrap(argv, ns)
        finally:
            sys.stdout, sys.stderr = saved
        return rc, err.value

    def test_captured_findings_yield_a_counted_reminder(self):
        msg = mm.adjudication_reminder(WORKED_SUMMARY, canary=False)
        self.assertIsNotNone(msg)
        self.assertIn("2 finding", msg)                          # WORKED_SUMMARY has 2
        self.assertIn("merge_gate_adjudicate.py adjudicate", msg)

    def test_nothing_to_adjudicate_is_silent(self):
        # A clean push (no findings) and a missing-state push (no summary) have
        # nothing for the human to judge → no reminder.
        self.assertIsNone(mm.adjudication_reminder({"findings": []}, canary=False))
        self.assertIsNone(mm.adjudication_reminder(None, canary=False))

    def test_canary_capture_is_silent(self):
        # Canary pushes are excluded from N and never adjudicated (design §4),
        # even when the produced summary carries findings.
        self.assertIsNone(mm.adjudication_reminder(WORKED_SUMMARY, canary=True))

    def test_findings_push_emits_reminder_through_verify_wrap(self):
        # The reminder surfaces on the real push path (the operator sees it in
        # the git push output), without breaking the push (rc passthrough).
        rc, err = self._drive(WORKED_SUMMARY, state="fresh", canary=False)
        self.assertEqual(rc, 0)                                  # behaviour-neutral
        self.assertIn("blind adjudication pending", err)
        self.assertIn("2 finding", err)

    def test_clean_push_emits_no_reminder_through_verify_wrap(self):
        # A fresh push whose review found nothing → no reminder noise.
        rc, err = self._drive({"verdict": "pass", "block_count": 0, "findings": []},
                              state="fresh", canary=False)
        self.assertEqual(rc, 0)
        self.assertNotIn("adjudication", err)


# --------------------------------------------------------------------------
# #33 G4 — a SIGINT during the in-flight-produce wait must still write a row
# (so an aborted push is not a silent measurement-data drop), and surface as a
# `wait-interrupted` flag rather than a clean state.
# --------------------------------------------------------------------------
class TestVerifyWaitInterruptRow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.measure = Path(self.tmp.name) / "measure"
        self._env = {"MERGE_GATE_MEASURE_DIR": str(self.measure),
                     "MERGE_GATE_STATE_ROOT": str(Path(self.tmp.name) / "state")}
        self._saved_env = {k: os.environ.get(k) for k in self._env}
        os.environ.update(self._env)
        self._saved_fn = {k: getattr(mm, k)
                          for k in ("run_real", "locate_summary", "_is_canary")}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in self._saved_fn.items():
            setattr(mm, k, v)

    def _drive_interrupt(self):
        def boom(argv):
            raise KeyboardInterrupt()
        mm.run_real = boom
        mm.locate_summary = lambda *a, **k: (None, None, None)  # artefact not written
        mm._is_canary = lambda root, tip: False
        argv = ["verify", "--base-sha", "0" * 40, "--tip-sha", "deadbeef"]
        ns = mm.build_parser().parse_args(["--cwd", str(self.root)] + argv)
        out, err = _Cap(), _Cap()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            return mm.cmd_verify_wrap(argv, ns)
        finally:
            sys.stdout, sys.stderr = saved

    def _ledger(self):
        p = self.measure / "log.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_sigint_during_wait_writes_interrupt_row_and_exits_130(self):
        rc = self._drive_interrupt()
        self.assertEqual(rc, 130)                       # SIGINT convention → push aborts
        led = self._ledger()
        self.assertIn("## push deadbeef", led)          # not a silent data drop
        self.assertIn("wait-interrupted", led)          # flagged, not a clean state
        self.assertIn("intervention: yes", led)         # an aborted push is operator burden


if __name__ == "__main__":
    unittest.main()
