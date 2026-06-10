#!/usr/bin/env python3
"""Regression suite for merge_gate_adjudicate.py — finding-first BLIND adjudication
UX (#31 Phase 1, Step 2).

Stdlib `unittest` only (pytest is not installed). Mirrors the measurement-wrapper
test discipline: parse a SAMPLE ledger built from the real schema (design §3), drive
each pure function, and assert the load-bearing blinding invariant — the pre-reveal
view shows the finding + its validator citation but NEVER the gate block flag /
verdict (so "would I have blocked this?" is not anchored to what the gate did).

Run:  python3 scripts/test_merge_gate_adjudicate.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import merge_gate_adjudicate as adj  # noqa: E402


# --------------------------------------------------------------------------
# A SAMPLE ledger built from the real schema (design §3) — two push sections.
# Section A: a block (verdict=block) with two findings, one already adjudicated
#            (human cells filled) and one pending (⬜); carries a citation sub-block.
# Section B: a pass (verdict=pass) with one pending finding, no citation sub-block.
# Header text and a template fence are included so the parser must ignore non-push
# noise exactly as the real ledger has it.
# --------------------------------------------------------------------------
SAMPLE = """\
# merge-gate measurement ledger — claude-config (local profile)

Some header prose that is not a push section.

---

## push aaaaaaaaaa · 2026-06-03T00:00:00Z · 54eec8f752..d210322521
- reviewers: codex, claude        verdict(gate): block · block_count=1
- freshness ⓐ: failing-findings   intervention: no
- latency: total=228s · codex r=30s v=89s · claude r=12s v=97s
- context: auto-produces-since-prev ⓑ=0 · intent/force ⓒ=—
- machine: codex_version=codex-cli 0.136.0 claude_model=claude-opus-4-8[1m] schema=1 enforcement=advisory
- human push-verdict: —   ← captured finding-first

| # | finding (file:line) | reviewers | sev | validator | block | conc | conf | rconf | human:real? | human:should-block? | same-as |
|---|---------------------|-----------|-----|-----------|-------|------|------|-------|-------------|---------------------|---------|
| 0 | scripts/merge_gate_local.py:1304 | codex | high | uphold | ✓ | 2 | 6 | 0.88 | ⬜ | ⬜ |  |
| 1 | scripts/merge_gate_local.py:1182 | claude | medium | dismiss | ✗ | 1 | 2 | 0.45 | yes | no | =0 |

- validator citations ⓐ (snapshot; source validators.{json,md} are GC-collected):
  - [0] codex:finding-0 · uphold · "Hook inheritance is harmless here" but session_devlog.py carries no MERGE_GATE_PRODUCER_RUNNING guard
  - [1] claude:finding-1 · dismiss · not k.startswith("CLAUDE_CODE_") strips CLAUDE_CODE_OAUTH_TOKEN; docstring only preserves ANTHROPIC_*

## push bbbbbbbbbb · 2026-06-03T01:00:00Z · cb1be9bf0e..9568aadf1c
- reviewers: codex, claude        verdict(gate): pass · block_count=0
- freshness ⓐ: fresh   intervention: no
- latency: total=100s · codex r=10s v=20s · claude r=30s v=40s
- context: auto-produces-since-prev ⓑ=0 · intent/force ⓒ=—
- machine: codex_version=codex-cli 0.136.0 claude_model=claude-opus-4-8[1m] schema=1 enforcement=advisory
- human push-verdict: —   ← captured finding-first

| # | finding (file:line) | reviewers | sev | validator | block | conc | conf | rconf | human:real? | human:should-block? | same-as |
|---|---------------------|-----------|-----|-----------|-------|------|------|-------|-------------|---------------------|---------|
| 0 | scripts/status.py:42 | claude | medium | dismiss | ✗ | 1 | 2 | 0.40 | ⬜ | ⬜ |  |
"""


# --------------------------------------------------------------------------
# 1. parse_ledger
# --------------------------------------------------------------------------
class TestParseLedger(unittest.TestCase):
    def setUp(self):
        self.sections = adj.parse_ledger(SAMPLE)

    def test_two_push_sections(self):
        self.assertEqual([s.push_id for s in self.sections], ["aaaaaaaaaa", "bbbbbbbbbb"])

    def test_section_machine_fields(self):
        a = self.sections[0]
        self.assertEqual(a.base, "54eec8f752")
        self.assertEqual(a.diff, "d210322521")
        self.assertEqual(a.verdict, "block")
        self.assertEqual(a.block_count, 1)
        self.assertEqual(a.freshness, "failing-findings")
        self.assertEqual(a.intervention, "no")
        self.assertEqual(a.push_verdict, "—")

    def test_pass_section_verdict(self):
        b = self.sections[1]
        self.assertEqual(b.verdict, "pass")
        self.assertEqual(b.block_count, 0)
        self.assertEqual(b.freshness, "fresh")

    def test_findings_parsed_with_all_columns(self):
        f = self.sections[0].findings[0]
        self.assertEqual(f.index, 0)
        self.assertEqual(f.location, "scripts/merge_gate_local.py:1304")
        self.assertEqual(f.reviewers, "codex")
        self.assertEqual(f.severity, "high")
        self.assertEqual(f.validator_verdict, "uphold")
        self.assertTrue(f.block)
        self.assertEqual(f.conc, "2")
        self.assertEqual(f.conf, "6")
        self.assertEqual(f.rconf, "0.88")

    def test_block_flag_bool(self):
        self.assertTrue(self.sections[0].findings[0].block)   # ✓
        self.assertFalse(self.sections[0].findings[1].block)  # ✗

    def test_human_cells_parsed(self):
        # row 0 is blank (⬜); row 1 is already adjudicated.
        f0, f1 = self.sections[0].findings
        self.assertIn(f0.human_real, ("", "⬜"))
        self.assertIn(f0.human_should_block, ("", "⬜"))
        self.assertEqual(f1.human_real, "yes")
        self.assertEqual(f1.human_should_block, "no")
        self.assertEqual(f1.same_as, "=0")

    def test_citation_joined_by_index_and_fid(self):
        f0, f1 = self.sections[0].findings
        self.assertIn("Hook inheritance is harmless here", f0.citation)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", f1.citation)

    def test_section_without_citation_block(self):
        # Section B has no citation sub-block — finding.citation is empty, not an error.
        self.assertEqual(self.sections[1].findings[0].citation, "")

    def test_fenced_template_is_not_parsed_as_a_push(self):
        # The real ledger carries a per-push *template* inside a ``` fence whose
        # header is "## push <id> · <iso-utc> · ...". It must NOT become a section.
        fenced = (
            "## Per-push section template\n"
            "```\n"
            "## push <id> · <iso-utc> · <base10>..<diff10>\n"
            "- reviewers: codex, claude        verdict(gate): <pass|block> · block_count=<N>\n"
            "- human push-verdict: <would-block | would-pass | —>   ← captured finding-first\n"
            "```\n\n" + SAMPLE
        )
        ids = [s.push_id for s in adj.parse_ledger(fenced)]
        self.assertEqual(ids, ["aaaaaaaaaa", "bbbbbbbbbb"])
        self.assertNotIn("<id>", ids)


# --------------------------------------------------------------------------
# 2. blind_view — the blinding invariant
# --------------------------------------------------------------------------
class TestBlindView(unittest.TestCase):
    def setUp(self):
        self.f = adj.parse_ledger(SAMPLE)[0].findings[0]  # the block finding w/ citation

    def test_pre_reveal_has_location_reviewers_citation(self):
        pre, _post = adj.blind_view(self.f)
        self.assertIn("scripts/merge_gate_local.py:1304", pre)
        self.assertIn("codex", pre)
        self.assertIn("Hook inheritance is harmless here", pre)

    def test_pre_reveal_does_not_leak_block_or_verdict(self):
        # THE load-bearing invariant: this test FAILS if someone leaks the gate
        # block flag / verdict into the pre-reveal view (design §3, §8).
        pre, _post = adj.blind_view(self.f)
        low = pre.lower()
        self.assertNotIn("✓", pre)
        self.assertNotIn("✗", pre)
        self.assertNotIn("block", low)
        self.assertNotIn("verdict", low)

    def test_pre_reveal_does_not_leak_severity_or_validator(self):
        # block = (severity ∈ {critical,high}) ∧ (verdict ∈ {uphold,unsure}) is
        # DETERMINISTIC, so exposing severity AND the validator verdict in the
        # pre-reveal lets the operator infer the gate's block decision before
        # judging — defeating blinding exactly as leaking block/verdict would
        # (agy third-party review, 2026-06-03). Both belong in post-reveal.
        pre, _post = adj.blind_view(self.f)
        low = pre.lower()
        self.assertNotIn("severity", low)
        self.assertNotIn("validator", low)
        self.assertNotIn("uphold", low)   # the validator verdict value itself
        self.assertNotIn("high", low)     # the severity value itself

    def test_post_reveal_contains_block_and_verdict(self):
        _pre, post = adj.blind_view(self.f)
        low = post.lower()
        self.assertIn("block", low)
        # the gate's mechanical decision ✓ (block=true) must be revealed post-hoc.
        self.assertIn("✓", post)

    def test_post_reveal_reveals_severity_and_validator(self):
        # severity + validator are determinant of the block decision, so they are
        # revealed only AFTER the human verdict — alongside the block flag.
        _pre, post = adj.blind_view(self.f)
        low = post.lower()
        self.assertIn("high", low)      # severity
        self.assertIn("uphold", low)    # validator verdict

    def test_dismissed_finding_post_reveal_shows_not_blocked(self):
        f1 = adj.parse_ledger(SAMPLE)[0].findings[1]
        _pre, post = adj.blind_view(f1)
        self.assertIn("✗", post)


# --------------------------------------------------------------------------
# 3. pending_findings
# --------------------------------------------------------------------------
class TestPendingFindings(unittest.TestCase):
    def test_only_blank_human_cells_are_pending(self):
        sections = adj.parse_ledger(SAMPLE)
        pending = adj.pending_findings(sections)
        # (push, finding) pairs: section A finding 0 (blank), section B finding 0 (blank).
        # Section A finding 1 is already adjudicated → skipped.
        keys = [(pid, f.index) for pid, f in pending]
        self.assertEqual(keys, [("aaaaaaaaaa", 0), ("bbbbbbbbbb", 0)])

    def test_fully_adjudicated_ledger_yields_nothing(self):
        sections = adj.parse_ledger(SAMPLE)
        # adjudicate both pending findings, then no findings remain pending.
        text = SAMPLE
        text = adj.apply_finding_verdict(text, "aaaaaaaaaa", 0, "yes", "yes", "")
        text = adj.apply_finding_verdict(text, "bbbbbbbbbb", 0, "no", "no", "")
        self.assertEqual(adj.pending_findings(adj.parse_ledger(text)), [])


# --------------------------------------------------------------------------
# 4. apply_finding_verdict — surgical, idempotent
# --------------------------------------------------------------------------
class TestApplyFindingVerdict(unittest.TestCase):
    def test_sets_only_the_target_row_human_cells(self):
        out = adj.apply_finding_verdict(SAMPLE, "aaaaaaaaaa", 0, "no", "no", "")
        secs = adj.parse_ledger(out)
        f0 = secs[0].findings[0]
        self.assertEqual(f0.human_real, "no")
        self.assertEqual(f0.human_should_block, "no")

    def test_non_target_row_byte_identical(self):
        # The already-adjudicated row 1 of section A must be untouched, byte for byte.
        target_line = "| 1 | scripts/merge_gate_local.py:1182 | claude | medium | dismiss | ✗ | 1 | 2 | 0.45 | yes | no | =0 |"
        self.assertIn(target_line, SAMPLE)
        out = adj.apply_finding_verdict(SAMPLE, "aaaaaaaaaa", 0, "yes", "yes", "=1")
        self.assertIn(target_line, out)
        # And section B's finding row is untouched too.
        b_line = "| 0 | scripts/status.py:42 | claude | medium | dismiss | ✗ | 1 | 2 | 0.40 | ⬜ | ⬜ |  |"
        self.assertIn(b_line, SAMPLE)
        self.assertIn(b_line, out)

    def test_same_as_written(self):
        out = adj.apply_finding_verdict(SAMPLE, "aaaaaaaaaa", 0, "yes", "yes", "=1")
        f0 = adj.parse_ledger(out).findings_for("aaaaaaaaaa")[0]
        self.assertEqual(f0.same_as, "=1")

    def test_idempotent(self):
        once = adj.apply_finding_verdict(SAMPLE, "aaaaaaaaaa", 0, "no", "yes", "=2")
        twice = adj.apply_finding_verdict(once, "aaaaaaaaaa", 0, "no", "yes", "=2")
        self.assertEqual(once, twice)

    def test_only_changed_bytes_are_the_target_row(self):
        out = adj.apply_finding_verdict(SAMPLE, "bbbbbbbbbb", 0, "no", "no", "")
        # Exactly one line differs between SAMPLE and out.
        diff = [(a, b) for a, b in zip(SAMPLE.splitlines(), out.splitlines()) if a != b]
        self.assertEqual(len(diff), 1)
        self.assertIn("scripts/status.py:42", diff[0][0])

    def test_disambiguates_findings_across_pushes(self):
        # Section A and B both have a finding index 0 — the push_id must select.
        out = adj.apply_finding_verdict(SAMPLE, "bbbbbbbbbb", 0, "real-b", "sb-b", "sa-b")
        a0 = adj.parse_ledger(out).findings_for("aaaaaaaaaa")[0]
        b0 = adj.parse_ledger(out).findings_for("bbbbbbbbbb")[0]
        self.assertIn(a0.human_real, ("", "⬜"))   # section A untouched
        self.assertEqual(b0.human_real, "real-b")


# --------------------------------------------------------------------------
# 5. apply_push_verdict
# --------------------------------------------------------------------------
class TestApplyPushVerdict(unittest.TestCase):
    def test_sets_section_push_verdict(self):
        out = adj.apply_push_verdict(SAMPLE, "aaaaaaaaaa", "would-block")
        self.assertEqual(adj.parse_ledger(out).section("aaaaaaaaaa").push_verdict, "would-block")

    def test_other_section_unchanged(self):
        out = adj.apply_push_verdict(SAMPLE, "aaaaaaaaaa", "would-block")
        self.assertEqual(adj.parse_ledger(out).section("bbbbbbbbbb").push_verdict, "—")

    def test_preserves_finding_first_marker(self):
        out = adj.apply_push_verdict(SAMPLE, "bbbbbbbbbb", "would-pass")
        self.assertIn("- human push-verdict: would-pass   ← captured finding-first", out)

    def test_idempotent(self):
        once = adj.apply_push_verdict(SAMPLE, "aaaaaaaaaa", "would-block")
        twice = adj.apply_push_verdict(once, "aaaaaaaaaa", "would-block")
        self.assertEqual(once, twice)


# --------------------------------------------------------------------------
# 6. false-positive recoverability (design §8 detection handshake)
# --------------------------------------------------------------------------
class TestFalsePositive(unittest.TestCase):
    def test_block_plus_should_block_no_is_a_false_positive(self):
        # Adjudicate the block finding (gate block=true) as should-block?=no.
        out = adj.apply_finding_verdict(SAMPLE, "aaaaaaaaaa", 0, "no", "no", "")
        f = adj.parse_ledger(out).findings_for("aaaaaaaaaa")[0]
        self.assertTrue(f.block)
        self.assertEqual(f.human_should_block, "no")
        self.assertTrue(adj.is_false_positive(f))

    def test_block_upheld_by_human_is_not_fp(self):
        out = adj.apply_finding_verdict(SAMPLE, "aaaaaaaaaa", 0, "yes", "yes", "")
        f = adj.parse_ledger(out).findings_for("aaaaaaaaaa")[0]
        self.assertFalse(adj.is_false_positive(f))

    def test_non_block_should_block_no_is_not_fp(self):
        # A non-blocking finding the human also wouldn't block is not a false positive.
        f1 = adj.parse_ledger(SAMPLE).findings_for("aaaaaaaaaa")[1]  # block=✗, should-block=no
        self.assertFalse(adj.is_false_positive(f1))

    def test_false_positives_helper_lists_them(self):
        out = adj.apply_finding_verdict(SAMPLE, "aaaaaaaaaa", 0, "no", "no", "")
        fps = adj.false_positives(adj.parse_ledger(out))
        keys = [(pid, f.index) for pid, f in fps]
        self.assertEqual(keys, [("aaaaaaaaaa", 0)])


# --------------------------------------------------------------------------
# adjudicate — the thin interactive shell (ask/show injected)
# --------------------------------------------------------------------------
class TestAdjudicateLoop(unittest.TestCase):
    def test_citation_shown_before_any_block_or_verdict(self):
        shown: list[str] = []
        asked: list[str] = []

        def show(text):
            shown.append(text)

        def ask(prompt):
            asked.append(prompt)
            # answer real?/should-block?/same-as, then the push-verdict.
            low = prompt.lower()
            if "push-verdict" in low or "would-block" in low:
                return "would-pass"
            if "real" in low:
                return "no"
            if "should-block" in low:
                return "no"
            if "same-as" in low:
                return ""
            return ""

        out = adj.adjudicate(SAMPLE, ask, show)

        # (a) the citation was shown, and BEFORE any block flag / verdict was revealed.
        joined = "\n".join(shown)
        self.assertIn("Hook inheritance is harmless here", joined)
        first_citation = next(i for i, s in enumerate(shown)
                              if "Hook inheritance is harmless here" in s)
        first_reveal = next((i for i, s in enumerate(shown)
                             if "✓" in s or "✗" in s or "verdict" in s.lower()), None)
        self.assertIsNotNone(first_reveal, "post-reveal must happen at some point")
        self.assertLess(first_citation, first_reveal,
                        "citation must be shown BEFORE the gate block/verdict is revealed")

        # (b) the captured cells are filled in the resulting text.
        secs = adj.parse_ledger(out)
        f0 = secs.findings_for("aaaaaaaaaa")[0]
        self.assertEqual(f0.human_should_block, "no")
        self.assertEqual(adj.pending_findings(secs), [])

    def test_loop_skips_already_adjudicated_rows(self):
        seen_locations: list[str] = []

        def show(text):
            seen_locations.append(text)

        def ask(prompt):
            low = prompt.lower()
            if "push-verdict" in low:
                return "would-pass"
            if "should-block" in low:
                return "no"
            if "real" in low:
                return "no"
            return ""

        adj.adjudicate(SAMPLE, ask, show)
        joined = "\n".join(seen_locations)
        # The already-adjudicated row 1 (:1182) must never be presented.
        self.assertNotIn("scripts/merge_gate_local.py:1182", joined)
        # The pending rows were presented.
        self.assertIn("scripts/merge_gate_local.py:1304", joined)
        self.assertIn("scripts/status.py:42", joined)

    def test_no_pending_findings_is_a_noop(self):
        text = SAMPLE
        text = adj.apply_finding_verdict(text, "aaaaaaaaaa", 0, "yes", "yes", "")
        text = adj.apply_finding_verdict(text, "bbbbbbbbbb", 0, "no", "no", "")
        # also fill both push-verdicts so nothing is pending at all.
        text = adj.apply_push_verdict(text, "aaaaaaaaaa", "would-block")
        text = adj.apply_push_verdict(text, "bbbbbbbbbb", "would-pass")

        def ask(prompt):
            raise AssertionError("ask must not be called when nothing is pending")

        out = adj.adjudicate(text, ask, lambda _t: None)
        self.assertEqual(out, text)


# --------------------------------------------------------------------------
# 7. main(argv) — thin CLI wiring
# --------------------------------------------------------------------------
class TestMainCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "log.md"
        self.ledger.write_text(SAMPLE, encoding="utf-8")

    def test_adjudicate_subcommand_writes_back(self):
        answers = iter(["no", "no", "", "would-pass", "no", "no", "", "would-pass"])
        rc = adj.main(["adjudicate", "--ledger", str(self.ledger)],
                      ask=lambda _p: next(answers), show=lambda _t: None)
        self.assertEqual(rc, 0)
        out = self.ledger.read_text(encoding="utf-8")
        secs = adj.parse_ledger(out)
        self.assertEqual(adj.pending_findings(secs), [])
        self.assertEqual(secs.section("aaaaaaaaaa").findings[0].human_should_block, "no")

    def test_default_ledger_path_is_the_canonical_one(self):
        # main must NOT auto-run against the canonical ledger in tests — we only
        # assert the default PATH wiring, never invoking it.
        self.assertEqual(
            adj.DEFAULT_LEDGER,
            Path("/home/shine/.claude/.scratch/merge-gate-measurement/log.md"),
        )


# --------------------------------------------------------------------------
# #35 — advisory-path conformance recurrence (A: accept + measure)
# --------------------------------------------------------------------------
# Two organic rows: A degraded both ways (claude reviewer malformed-payload +
# a finding the validator dup-id guard forced to unsure), B clean.
ADVISORY_SAMPLE = """\
## push aaaaaaaaaa · 2026-06-04T00:00:00Z · 1111111111..2222222222
- reviewers: codex, claude        verdict(gate): error · block_count=0
- freshness ⓐ: failing-incomplete   intervention: no
- latency: total=10s · codex r=5s v=5s
- context: auto-produces-since-prev ⓑ=0 · intent/force ⓒ=—
- machine: codex_version=x claude_model=y schema=1 enforcement=advisory
- advisory conformance ⓓ: claude=malformed-payload (no usable signal — soft-steer #35)
- human push-verdict: —   ← captured finding-first

| # | finding (file:line) | reviewers | sev | validator | block | conc | conf | rconf | human:real? | human:should-block? | same-as |
|---|---------------------|-----------|-----|-----------|-------|------|------|-------|-------------|---------------------|---------|
| 0 | f.py:10 | codex | high | unsure | ✓ | 1 | 3 | 0.8 | ⬜ | ⬜ |  |

- validator citations ⓐ (snapshot; source validators.{json,md} are GC-collected):
  - [0] codex:finding-0 · unsure · duplicate id in validator output (count=2)

## push bbbbbbbbbb · 2026-06-04T01:00:00Z · 3333333333..4444444444
- reviewers: codex, claude        verdict(gate): pass · block_count=0
- freshness ⓐ: fresh   intervention: no
- latency: total=10s · codex r=5s v=5s
- context: auto-produces-since-prev ⓑ=0 · intent/force ⓒ=—
- machine: codex_version=x claude_model=y schema=1 enforcement=advisory
- human push-verdict: —   ← captured finding-first

| # | finding (file:line) | reviewers | sev | validator | block | conc | conf | rconf | human:real? | human:should-block? | same-as |
|---|---------------------|-----------|-----|-----------|-------|------|------|-------|-------------|---------------------|---------|
| 0 | g.py:5 | codex | high | uphold | ✓ | 1 | 3 | 0.9 | ⬜ | ⬜ |  |
"""


class TestPaddedFindingRowParsing(unittest.TestCase):
    """#35 — parse_ledger must read COLUMN-ALIGNED (padded) finding rows, not just
    the canonical single-space form. A markdown table formatter run over the
    committed ledger pads cells (`| 0   |`); pre-fix _FINDING_RE missed them, so
    every adjudicate function (incl. advisory_signal_stats) saw 0 findings on the
    real ledger."""

    PADDED = """\
## push pppppppppp · 2026-06-04T00:00:00Z · 1111111111..2222222222
- reviewers: codex        verdict(gate): block · block_count=1
- freshness ⓐ: failing-findings   intervention: no
- latency: total=10s · codex r=5s v=5s
- context: auto-produces-since-prev ⓑ=0 · intent/force ⓒ=—
- machine: codex_version=x claude_model=y schema=1 enforcement=advisory
- human push-verdict: —   ← captured finding-first

| #   | finding (file:line)        | reviewers | sev    | validator | block | conc | conf | rconf | human:real? | human:should-block? | same-as |
|-----|----------------------------|-----------|--------|-----------|-------|------|------|-------|-------------|---------------------|---------|
| 0   | scripts/x.py:9             | codex     | high   | unsure    | ✓     | 1    | 3    | 0.8   | ⬜          | ⬜                  |         |

- validator citations ⓐ (snapshot; source validators.{json,md} are GC-collected):
  - [0] codex:finding-0 · unsure · duplicate id in validator output (count=2)
"""

    def test_padded_finding_row_parses_with_citation(self):
        secs = adj.parse_ledger(self.PADDED)
        self.assertEqual(len(secs[0].findings), 1)
        f = secs[0].findings[0]
        self.assertEqual(f.index, 0)
        self.assertEqual(f.location, "scripts/x.py:9")
        self.assertEqual(f.severity, "high")
        self.assertEqual(f.validator_verdict, "unsure")
        self.assertIn("duplicate id in validator output", f.citation)
        self.assertTrue(adj.is_forced_unsure(f))

    def test_header_and_separator_still_excluded(self):
        # The relaxed regex must NOT match the header (first cell "#") or the
        # separator (first cell "-----").
        secs = adj.parse_ledger(self.PADDED)
        self.assertEqual([f.index for f in secs[0].findings], [0])  # exactly the data row


class TestAdvisorySignalStats(unittest.TestCase):
    """#35 (A) — the ledger/adjudication path distinguishes 'no usable advisory
    signal' (reviewer malformed-payload; validator forced-unsure) from agreed/
    disagreed, and tracks the recurrence rate over organic N."""

    def setUp(self):
        self.sections = adj.parse_ledger(ADVISORY_SAMPLE)

    def test_parses_advisory_conformance_line(self):
        self.assertEqual(self.sections[0].advisory, "claude=malformed-payload")
        self.assertEqual(self.sections[1].advisory, "")

    def test_is_forced_unsure_detects_validator_dupid(self):
        self.assertTrue(adj.is_forced_unsure(self.sections[0].findings[0]))
        # the clean pass row's genuine uphold is NOT forced-unsure
        self.assertFalse(adj.is_forced_unsure(self.sections[1].findings[0]))

    def test_codex_side_dupid_is_out_of_scope(self):
        # #29 Codex-side dup-id ("duplicate Codex finding id") is explicitly NOT a
        # #35 case — only the validator-side guard counts.
        f = self.sections[0].findings[0]
        f.citation = "duplicate Codex finding id (count=2)"
        self.assertFalse(adj.is_forced_unsure(f))

    def test_genuine_unsure_is_not_forced(self):
        f = self.sections[0].findings[0]
        f.citation = "validator genuinely uncertain about exploitability"
        self.assertFalse(adj.is_forced_unsure(f))

    def test_stats_counts_both_modes_over_organic_rows(self):
        stats = adj.advisory_signal_stats(self.sections)
        self.assertEqual(stats["organic_rows"], 2)
        self.assertEqual(stats["reviewer_no_signal_rows"], 1)
        self.assertEqual(stats["forced_unsure_rows"], 1)
        self.assertEqual(stats["forced_unsure_findings"], 1)
        self.assertEqual(stats["reviewer_no_signal"],
                         [("aaaaaaaaaa", "claude=malformed-payload")])
        self.assertEqual(stats["forced_unsure"], [("aaaaaaaaaa", 0, "f.py:10")])

    def test_non_produced_row_is_not_organic(self):
        # A `verdict(gate): —` row (no produce — e.g. a missing artefact) is excluded.
        sections = adj.parse_ledger(ADVISORY_SAMPLE.replace(
            "verdict(gate): pass · block_count=0", "verdict(gate): — · block_count=—"))
        self.assertEqual(adj.advisory_signal_stats(sections)["organic_rows"], 1)

    def test_forced_unsure_excludes_non_organic_rows(self):
        # Regression (workflow finding): forced-unsure must be counted over ORGANIC
        # rows only. If the dup-id row's verdict is non-produced ('—'), its
        # forced-unsure finding must NOT count (it iterates `rows`, not all sections).
        text = ADVISORY_SAMPLE.replace(
            "verdict(gate): error · block_count=0", "verdict(gate): — · block_count=—")
        stats = adj.advisory_signal_stats(adj.parse_ledger(text))
        self.assertEqual(stats["organic_rows"], 1)         # only the pass row
        self.assertEqual(stats["forced_unsure_rows"], 0)   # dup-id row is now non-organic
        self.assertEqual(stats["forced_unsure_findings"], 0)

    def test_advisory_stats_cli_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.md"
            p.write_text(ADVISORY_SAMPLE)
            out = []
            rc = adj.main(["advisory-stats", "--ledger", str(p)],
                          ask=lambda _q: "", show=out.append)
            self.assertEqual(rc, 0)
            self.assertEqual(p.read_text(), ADVISORY_SAMPLE)  # never mutates the ledger
            text = "\n".join(out)
            self.assertIn("organic rows (produced):", text)
            self.assertIn("reviewer no-usable-signal rows:", text)
            self.assertIn("validator forced-unsure rows:", text)
            self.assertIn("1 / 2", text)            # the rate (1 of 2 organic rows)
            self.assertIn("(1 finding(s))", text)   # granular detail


class TestLedgerWriteSafety(unittest.TestCase):
    """Regression for the data-loss crash (UnicodeEncodeError mid-write that lost a
    hand-entered adjudication run): (1) writes are ATOMIC so a failure never
    truncates the canonical ledger; (2) captured verdict tokens are scrubbed of
    surrogates/control chars so a non-UTF-8 terminal can't poison the encoding."""

    def test_scrub_strips_surrogates_and_controls(self):
        self.assertEqual(adj._scrub_input("no"), "no")
        self.assertEqual(adj._scrub_input("ye\udce9s"), "yes")   # lone surrogate dropped
        self.assertEqual(adj._scrub_input("a\x00b\x07c"), "abc")  # control chars dropped
        self.assertEqual(adj._scrub_input("would-pass"), "would-pass")
        self.assertEqual(adj._scrub_input("café"), "café")        # legit non-ASCII kept
        self.assertEqual(adj._scrub_input("a\tb"), "a\tb")        # tab kept

    def test_atomic_write_replaces_content_no_tmp_litter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.md"
            p.write_text("old", encoding="utf-8")
            adj._atomic_write_text(p, "new content\n")
            self.assertEqual(p.read_text(encoding="utf-8"), "new content\n")
            self.assertEqual(list(Path(d).glob(".log.md.tmp-*")), [])

    def test_atomic_write_preserves_original_on_encode_failure(self):
        # THE data-loss regression: a surrogate in the payload must NOT truncate or
        # empty the existing ledger — the encode fails on the temp file, the original
        # is left byte-for-byte intact, and no temp litter remains.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.md"
            original = "ORIGINAL LEDGER — must survive\n"
            p.write_text(original, encoding="utf-8")
            with self.assertRaises(UnicodeEncodeError):
                adj._atomic_write_text(p, "before\udce9after")  # lone surrogate
            self.assertEqual(p.read_text(encoding="utf-8"), original)  # untouched
            self.assertEqual(list(Path(d).glob(".log.md.tmp-*")), [])

    def test_main_scrubs_surrogate_input_and_writes_clean(self):
        # End-to-end: a surrogate smuggled into a captured verdict is scrubbed, the
        # write succeeds, and the ledger stays valid UTF-8 with the clean verdict.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.md"
            p.write_text(SAMPLE, encoding="utf-8")
            # SAMPLE pending: push aaaa #0, push bbbb #0. asks: 3 per finding + 2
            # push-verdicts = 8. Smuggle a lone surrogate into the first answer.
            answers = iter(["no\udce9", "no", "",  "yes", "no", "",
                            "would-pass", "would-pass"])
            rc = adj.main(["adjudicate", "--ledger", str(p)],
                          ask=lambda _q: next(answers), show=lambda _t: None)
            self.assertEqual(rc, 0)
            written = p.read_text(encoding="utf-8")          # valid UTF-8 (no crash)
            secs = adj.parse_ledger(written)
            f0 = secs.section("aaaaaaaaaa").findings[0]
            self.assertEqual(f0.human_real, "no")            # scrubbed, not "no\udce9"
            self.assertEqual(f0.human_should_block, "no")


if __name__ == "__main__":
    unittest.main(verbosity=2)
