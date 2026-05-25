#!/usr/bin/env python3
"""Regression suite for aggregate.py — Codex review pipeline (refs claude-harness-work#18).

Stdlib `unittest` only — pytest is not installed in this environment.
Run:  python3 scripts/test_aggregate.py -v

Covers:
  - aggregate.py build-input on the workflow's NORMALIZED single-doc
    payload (the happy path the workflow's Normalize step produces).
  - aggregate.py build-input on a raw JSONL stream (defensive path for
    direct invocation outside the workflow).
  - Invariance: same findings from JSONL and from its workflow-normalize
    output. Proves the two extraction paths can't drift.
  - Fallbacks: no agent_message event, malformed agent_message.text,
    empty findings — each returns [] with exit 0.
  - The workflow's jq Normalize expression is duplicated as JQ_NORMALIZE
    and exercised against the same JSONL fixture; its output, fed back
    through aggregate.py, must match the JSONL path.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
AGGREGATE = SCRIPTS / "aggregate.py"
FIXTURES = SCRIPTS / "fixtures"

# Import parse_validator_output directly for unit tests on the pure function.
sys.path.insert(0, str(SCRIPTS))
from aggregate import parse_validator_output  # noqa: E402

# The workflow's "Normalize Codex JSONL → single-doc review payload" step
# jq expression. Kept here as a string so the test can prove byte-equivalence
# between (a) JSONL → workflow-normalize → aggregate.py and (b) JSONL →
# aggregate.py direct. If you edit this constant, update
# ~/.claude/skills/setup-merge-gate/templates/codex-review.yml's "Normalize
# Codex JSONL" step to match (and vice versa).
JQ_NORMALIZE = r"""
if $codex_exit != "0" then
  {result: {verdict: "unknown",
            summary: ("Codex exited with status " + $codex_exit + ". See codex-review.stderr in the run artefact."),
            findings: [], next_steps: []},
   codex: {status: "codex-failed", exit: ($codex_exit | tonumber? // -1)}}
else
  (map(select(.type == "item.completed"
              and ((.item // {}).type // "") == "agent_message"))
   | last) as $msg
  | if $msg == null then
      {result: {verdict: "unknown",
                summary: "Codex returned no agent_message event in its JSONL stream.",
                findings: [], next_steps: []},
       codex: {status: "missing-result"}}
    else
      (try ($msg.item.text | fromjson) catch null) as $payload
      | if $payload == null then
          {result: {verdict: "unknown",
                    summary: "Codex agent_message.text was not valid JSON conforming to review-output.schema.json.",
                    findings: [], next_steps: []},
           codex: {status: "malformed-payload"}}
        else
          {result: $payload, codex: {status: "ok"}}
        end
    end
end
"""


def run_build_input(codex_json: Path, issue_ref: str = "test-issue") -> dict:
    """Invoke aggregate.py build-input as a subprocess; return parsed stdout.

    Asserts exit 0 and parses stdout as JSON. The runtime contract is "never
    block from inside this script," so non-zero exit is itself a regression.
    """
    r = subprocess.run(
        ["python3", str(AGGREGATE), "build-input",
         "--codex-json", str(codex_json),
         "--issue-ref", issue_ref],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        raise AssertionError(
            f"aggregate.py exit={r.returncode}; stderr={r.stderr!r}"
        )
    return json.loads(r.stdout)


def jq_normalize(codex_json: Path, codex_exit: str = "0") -> dict:
    """Apply the workflow's Normalize jq expression to a JSONL file.

    Returns the parsed single-doc payload. Requires `jq` on $PATH.
    """
    r = subprocess.run(
        ["jq", "-s", "--arg", "codex_exit", codex_exit, JQ_NORMALIZE,
         str(codex_json)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise AssertionError(f"jq exit={r.returncode}; stderr={r.stderr!r}")
    return json.loads(r.stdout)


def write_tmp_json(tmpdir: Path, name: str, doc: dict) -> Path:
    p = tmpdir / name
    p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fixture discovery sanity
# ---------------------------------------------------------------------------

class TestFixturesPresent(unittest.TestCase):
    def test_all_fixtures_readable(self):
        expected = {
            "real_jsonl_with_findings.jsonl",
            "jsonl_no_agent_message.jsonl",
            "jsonl_empty_findings.jsonl",
            "jsonl_malformed_payload.jsonl",
            "single_doc_with_result.json",
            "validator-prose-preamble.txt",
        }
        actual = {p.name for p in FIXTURES.iterdir() if p.is_file()}
        self.assertEqual(expected, actual & expected,
                         f"missing fixtures: {expected - actual}")


# ---------------------------------------------------------------------------
# parse_validator_output: robustness against agent guardrail violations
# ---------------------------------------------------------------------------

class TestParseValidatorOutput(unittest.TestCase):
    """Regression suite for parse_validator_output (refs claude-harness-work#22).

    The validator agent's <output_contract> forbids prose preamble, but real
    observed runs (chess_transformer PR #8 run 26380160257) showed the agent
    writing a paragraph of analysis before the first [SEV] line. The parser
    must scan the full stdout and not bail at the first blank line.
    """

    def test_prose_preamble_does_not_lose_finding(self):
        """PR #8 raw_stdout regression — prose paragraph + blank + [SEV] line.

        Before the fix: blank-line-stop made parser parse only the prose
        paragraph, return [], and the cmd_write_outputs fail-safe synthesized
        a bogus `unsure` verdict even though the agent emitted `uphold`.
        """
        fixture = (FIXTURES / "validator-prose-preamble.txt").read_text()
        parsed = parse_validator_output(fixture)
        self.assertEqual(len(parsed), 1, f"expected 1 finding line, got {parsed}")
        self.assertEqual(parsed[0]["sev"], "critical")
        self.assertEqual(parsed[0]["verdict"], "uphold")
        self.assertEqual(parsed[0]["file"],
                         "src/engine/_smoke_upload_artefact_canary.py")
        self.assertEqual(parsed[0]["line"], 17)

    def test_lines_only_no_prose(self):
        """Spec-compliant stdout (no prose, no trailing summary) still parses."""
        text = (
            "[HIGH] uphold src/a.py:10 — src/a.py:10: bad pattern\n"
            "[LOW] dismiss src/b.py:5 — docs/adr/0001.md:3: explicitly allowed\n"
        )
        parsed = parse_validator_output(text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["verdict"], "uphold")
        self.assertEqual(parsed[1]["verdict"], "dismiss")

    def test_summary_block_is_skipped(self):
        """The 3-line summary block (block_count/bypass_eligible/action) is
        recomputed by aggregate.py and must not be parsed as a finding."""
        text = (
            "[HIGH] uphold src/a.py:10 — src/a.py:10: bad pattern\n"
            "\n"
            "block_count: 1\n"
            "bypass_eligible: no\n"
            "action: apply fixes for 1 upheld finding(s)\n"
        )
        parsed = parse_validator_output(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["verdict"], "uphold")

    def test_prose_trailing_does_not_create_extra_findings(self):
        """If the agent writes prose AFTER the [SEV] lines (also a contract
        violation), the parser still captures only the matched lines."""
        text = (
            "[MEDIUM] unsure src/c.py:42 — no ADR covers magic-number policy\n"
            "\n"
            "Note: I considered widening the search but found no further evidence.\n"
        )
        parsed = parse_validator_output(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["verdict"], "unsure")
        self.assertEqual(parsed[0]["sev"], "medium")

    def test_empty_text_returns_empty(self):
        self.assertEqual(parse_validator_output(""), [])


# ---------------------------------------------------------------------------
# build-input: happy paths
# ---------------------------------------------------------------------------

class TestBuildInput(unittest.TestCase):
    def test_single_doc_with_result(self):
        """aggregate.py read of the normalize-step output."""
        payload = run_build_input(FIXTURES / "single_doc_with_result.json")
        self.assertEqual(payload["issue_ref"], "test-issue")
        findings = payload["codex_json"]["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "critical")
        self.assertEqual(findings[0]["file"], "injected.py")
        self.assertEqual(findings[0]["line"], 4)
        self.assertIn("Arbitrary shell command execution", findings[0]["title"])

    def test_raw_jsonl_defensive_path(self):
        """aggregate.py invoked directly on raw JSONL — must take the LAST
        agent_message (the second one in the fixture has the real finding)."""
        payload = run_build_input(FIXTURES / "real_jsonl_with_findings.jsonl")
        findings = payload["codex_json"]["findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "critical")
        self.assertEqual(findings[0]["file"], "injected.py")
        self.assertEqual(findings[0]["line"], 4)


# ---------------------------------------------------------------------------
# Invariance: JSONL ≡ workflow-normalized single-doc
# ---------------------------------------------------------------------------

class TestNormalizeInvariance(unittest.TestCase):
    """The workflow's Normalize jq + aggregate.py(single-doc) must produce
    the same findings as aggregate.py(JSONL) on the same JSONL input.

    If they diverge, the workflow path silently drops or duplicates
    findings — exactly the #18 regression class.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aggregatetest-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_jsonl_then_workflow_path_matches_direct_jsonl_path(self):
        src = FIXTURES / "real_jsonl_with_findings.jsonl"
        # Path A — direct: aggregate.py on raw JSONL (defensive in-process).
        direct = run_build_input(src)
        # Path B — workflow: jq Normalize first, then aggregate.py on single-doc.
        normalized = jq_normalize(src, codex_exit="0")
        self.assertEqual(normalized["codex"]["status"], "ok")
        norm_path = write_tmp_json(self.tmpdir, "normalized.json", normalized)
        via_workflow = run_build_input(norm_path)
        self.assertEqual(direct["codex_json"]["findings"],
                         via_workflow["codex_json"]["findings"])


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------

class TestFallbacks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aggregatetest-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_jsonl_no_agent_message(self):
        """File has command_execution events but no agent_message at all."""
        payload = run_build_input(FIXTURES / "jsonl_no_agent_message.jsonl")
        self.assertEqual(payload["codex_json"]["findings"], [])

    def test_jsonl_empty_findings(self):
        """agent_message present, payload says verdict=approve, findings=[]."""
        payload = run_build_input(FIXTURES / "jsonl_empty_findings.jsonl")
        self.assertEqual(payload["codex_json"]["findings"], [])

    def test_jsonl_malformed_payload(self):
        """agent_message present but .item.text is not valid JSON."""
        payload = run_build_input(FIXTURES / "jsonl_malformed_payload.jsonl")
        self.assertEqual(payload["codex_json"]["findings"], [])

    def test_empty_file(self):
        """Zero-byte file — aggregate.py returns no findings, exit 0."""
        empty = self.tmpdir / "empty.json"
        empty.write_text("", encoding="utf-8")
        payload = run_build_input(empty)
        self.assertEqual(payload["codex_json"]["findings"], [])

    def test_single_doc_without_result_key(self):
        """Single-doc payload that doesn't match the expected shape."""
        p = write_tmp_json(self.tmpdir, "weird.json", {"some": "other shape"})
        payload = run_build_input(p)
        self.assertEqual(payload["codex_json"]["findings"], [])


# ---------------------------------------------------------------------------
# Workflow normalize step semantics
# ---------------------------------------------------------------------------

class TestNormalizeExpression(unittest.TestCase):
    """Direct tests for the jq expression the workflow runs. Catches drift
    between the constant in this file and the YAML template.
    """

    def test_ok_status_for_real_jsonl(self):
        norm = jq_normalize(FIXTURES / "real_jsonl_with_findings.jsonl")
        self.assertEqual(norm["codex"]["status"], "ok")
        self.assertEqual(norm["result"]["verdict"], "needs-attention")
        self.assertEqual(len(norm["result"]["findings"]), 1)

    def test_codex_failed_when_exit_nonzero(self):
        norm = jq_normalize(FIXTURES / "real_jsonl_with_findings.jsonl",
                            codex_exit="42")
        self.assertEqual(norm["codex"]["status"], "codex-failed")
        self.assertEqual(norm["codex"]["exit"], 42)
        self.assertEqual(norm["result"]["findings"], [])

    def test_missing_result_when_no_agent_message(self):
        norm = jq_normalize(FIXTURES / "jsonl_no_agent_message.jsonl")
        self.assertEqual(norm["codex"]["status"], "missing-result")

    def test_malformed_payload_when_text_not_json(self):
        norm = jq_normalize(FIXTURES / "jsonl_malformed_payload.jsonl")
        self.assertEqual(norm["codex"]["status"], "malformed-payload")

    def test_last_agent_message_wins(self):
        """Real fixture has TWO agent_messages — preliminary (empty findings)
        and final (one critical finding). The normalize step must pick the
        last; otherwise we'd report 0 findings on every actual review."""
        norm = jq_normalize(FIXTURES / "real_jsonl_with_findings.jsonl")
        # If the first agent_message won, summary would mention "Inspecting".
        # The last one's summary mentions "diff adds injected.py".
        self.assertIn("injected.py", norm["result"]["summary"])
        self.assertEqual(len(norm["result"]["findings"]), 1)


# ---------------------------------------------------------------------------
# write-fallback subcommand (defensive — already tested via #15, recheck here)
# ---------------------------------------------------------------------------

class TestWriteFallback(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aggregatetest-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_fallback_produces_valid_artifacts(self):
        r = subprocess.run(
            ["python3", str(AGGREGATE), "write-fallback",
             "--reason", "test reason",
             "--out-dir", str(self.tmpdir)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        vj = json.loads((self.tmpdir / "validators.json").read_text())
        self.assertEqual(vj["validators"], [])
        self.assertEqual(vj["aggregate"], [])
        self.assertEqual(vj["fallback"], "test reason")
        vmd = (self.tmpdir / "validators.md").read_text()
        self.assertIn("test reason", vmd)


if __name__ == "__main__":
    if not AGGREGATE.exists():
        raise SystemExit(f"aggregate.py not found at {AGGREGATE}")
    if shutil.which("jq") is None:
        raise SystemExit("`jq` not on $PATH — install jq to run these tests.")
    unittest.main(verbosity=2)
