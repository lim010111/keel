#!/usr/bin/env python3
"""Regression suite for aggregate.py — Codex review pipeline (refs claude-harness-work#18).

Stdlib `unittest` only — pytest is not installed in this environment.
Run:  python3 scripts/test_aggregate.py -v

Covers:
  - aggregate.py build-input on the producer's NORMALIZED single-doc
    payload (the happy path the Normalize step produces).
  - aggregate.py build-input on a raw JSONL stream (defensive path for
    direct invocation outside the producer).
  - Invariance: same findings from JSONL and from its normalize output.
    Proves the two extraction paths can't drift.
  - Fallbacks: no agent_message event, malformed agent_message.text,
    empty findings — each returns [] with exit 0.
  - The GHA-era jq Normalize expression is duplicated as JQ_NORMALIZE
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

# The GHA-era "Normalize Codex JSONL → single-doc review payload" jq
# expression (the workflow it shipped in was removed — ADR-0021). Kept here
# verbatim as the reference oracle so the test can prove byte-equivalence
# between (a) JSONL → jq-normalize → aggregate.py and (b) JSONL →
# aggregate.py direct. merge_gate_local.normalize_codex_jsonl is the live
# port; test_merge_gate_local._JQ_NORMALIZE mirrors this constant.
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
            "[HIGH] uphold id=f1 src/a.py:10 — src/a.py:10: bad pattern\n"
            "[LOW] dismiss id=f2 src/b.py:5 — docs/adr/0001.md:3: explicitly allowed\n"
        )
        parsed = parse_validator_output(text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["verdict"], "uphold")
        self.assertEqual(parsed[0]["id"], "f1")
        self.assertEqual(parsed[1]["verdict"], "dismiss")
        self.assertEqual(parsed[1]["id"], "f2")

    def test_summary_block_is_skipped(self):
        """The 3-line summary block (block_count/bypass_eligible/action) is
        recomputed by aggregate.py and must not be parsed as a finding."""
        text = (
            "[HIGH] uphold id=f1 src/a.py:10 — src/a.py:10: bad pattern\n"
            "\n"
            "block_count: 1\n"
            "bypass_eligible: no\n"
            "action: apply fixes for 1 upheld finding(s)\n"
        )
        parsed = parse_validator_output(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["verdict"], "uphold")
        self.assertEqual(parsed[0]["id"], "f1")

    def test_prose_trailing_does_not_create_extra_findings(self):
        """If the agent writes prose AFTER the [SEV] lines (also a contract
        violation), the parser still captures only the matched lines."""
        text = (
            "[MEDIUM] unsure id=f1 src/c.py:42 — no ADR covers magic-number policy\n"
            "\n"
            "Note: I considered widening the search but found no further evidence.\n"
        )
        parsed = parse_validator_output(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["verdict"], "unsure")
        self.assertEqual(parsed[0]["sev"], "medium")
        self.assertEqual(parsed[0]["id"], "f1")

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

    def test_no_durable_context_by_default(self):
        """D11: without --durable-context-from the payload has NO
        durable_context key — intent-less invocations stay byte-identical
        to pre-#30."""
        payload = run_build_input(FIXTURES / "single_doc_with_result.json")
        self.assertNotIn("durable_context", payload)

    def test_durable_context_threaded_when_provided(self):
        """D11: --durable-context-from adds the durable_context field (local
        profile feeds branch / commit messages / operator intent)."""
        tmp = Path(tempfile.mkdtemp(prefix="aggregatetest-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ctx = tmp / "intent.txt"
        ctx.write_text("Branch: feature/x\n\nOperator intent:\nguard the auth path")
        r = subprocess.run(
            ["python3", str(AGGREGATE), "build-input",
             "--codex-json", str(FIXTURES / "single_doc_with_result.json"),
             "--issue-ref", "branch feature/x",
             "--durable-context-from", str(ctx)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertIn("durable_context", payload)
        self.assertIn("guard the auth path", payload["durable_context"])

    def test_empty_durable_context_omits_key(self):
        """An empty durable-context file leaves the payload byte-identical."""
        tmp = Path(tempfile.mkdtemp(prefix="aggregatetest-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ctx = tmp / "empty.txt"
        ctx.write_text("   \n")
        r = subprocess.run(
            ["python3", str(AGGREGATE), "build-input",
             "--codex-json", str(FIXTURES / "single_doc_with_result.json"),
             "--issue-ref", "x", "--durable-context-from", str(ctx)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("durable_context", json.loads(r.stdout))


class TestOutDirThreading(unittest.TestCase):
    """The --out-dir arg lands the artefacts wherever the producer points it
    (local profile passes the per-reviewer tuple sub-dir). aggregate.py has
    no built-in default (--out-dir is required); the skill-level default
    `./.merge-gate/` lives in SKILL.md prose, and the executor resolves it
    and passes --out-dir explicitly (#46)."""

    def test_write_outputs_honors_out_dir(self):
        tmp = Path(tempfile.mkdtemp(prefix="aggregatetest-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        codex = write_tmp_json(tmp, "codex.json", {
            "result": {"findings": [
                {"id": "codex:finding-0", "severity": "high", "file": "a.py",
                 "line_start": 3, "title": "t", "body": "b"}]},
            "codex": {"status": "ok"}})
        vout = tmp / "validator.txt"
        vout.write_text("[HIGH] uphold id=codex:finding-0 a.py:3 — real\n")
        out_dir = tmp / "deep" / "codex"
        r = subprocess.run(
            ["python3", str(AGGREGATE), "write-outputs",
             "--codex-json", str(codex), "--validator-output", str(vout),
             "--soft-mode", "false", "--out-dir", str(out_dir)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((out_dir / "validators.json").exists())
        self.assertTrue((out_dir / "validators.md").exists())
        agg = json.loads((out_dir / "validators.json").read_text())["aggregate"]
        self.assertEqual(agg[0]["finding_id"], "codex:finding-0")
        self.assertTrue(agg[0]["block"])

    def test_write_fallback_honors_out_dir(self):
        tmp = Path(tempfile.mkdtemp(prefix="aggregatetest-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out_dir = tmp / "nested" / "reviewer"
        r = subprocess.run(
            ["python3", str(AGGREGATE), "write-fallback",
             "--reason", "codex did not run", "--out-dir", str(out_dir)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((out_dir / "validators.json").exists())
        self.assertEqual(json.loads((out_dir / "validators.json").read_text())["fallback"],
                         "codex did not run")


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


# ---------------------------------------------------------------------------
# Identity-based pairing (#26 / ADR-0008)
# ---------------------------------------------------------------------------

class TestPairingOnId(unittest.TestCase):
    """Regression suite for identity-based validator/finding pairing
    (refs claude-harness-work#26, ADR-0008).

    The aggregator pairs each parsed validator line to its Codex finding
    via the explicit `id=<id>` token, then sanity-checks (file, line,
    severity). Positional pairing has been removed entirely — these
    tests are the lock against re-introducing it.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="pairingtest-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_write_outputs(
        self,
        codex_findings: list[dict],
        validator_output: str,
        soft_mode: str = "false",
    ) -> tuple[subprocess.CompletedProcess, dict]:
        """Invoke `aggregate.py write-outputs` against synthetic inputs.

        Returns (completed_process, parsed_validators_json).
        """
        codex_json = write_tmp_json(
            self.tmpdir,
            "codex.json",
            {"result": {"findings": codex_findings}, "codex": {"status": "ok"}},
        )
        validator_out = self.tmpdir / "validator.out"
        validator_out.write_text(validator_output, encoding="utf-8")
        out_dir = self.tmpdir / "out"
        r = subprocess.run(
            ["python3", str(AGGREGATE), "write-outputs",
             "--codex-json", str(codex_json),
             "--validator-output", str(validator_out),
             "--soft-mode", soft_mode,
             "--out-dir", str(out_dir)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(r.returncode, 0, f"aggregate.py exited non-zero: {r.stderr!r}")
        vj = json.loads((out_dir / "validators.json").read_text())
        return r, vj

    def test_reordered_validator_lines_pair_by_id(self):
        """AC: two findings, validator emits verdicts in reverse order.
        HIGH `uphold` must still land on the HIGH Codex finding (block=true
        survives the reorder). Without identity-based pairing, this test
        catches the #26 silent-swap regression.
        """
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
            {"id": "f2", "severity": "low", "file": "src/b.py", "line_start": 5,
             "title": "minor", "body": "..."},
        ]
        # Validator emits f2 first, then f1 — REVERSED from input order.
        validator_out = (
            "[LOW] dismiss id=f2 src/b.py:5 — docs/adr/0001.md:3: allowed\n"
            "[HIGH] uphold id=f1 src/a.py:10 — src/a.py:10: bad pattern\n"
        )
        _, vj = self._run_write_outputs(findings, validator_out)
        agg = {a["finding_id"]: a for a in vj["aggregate"]}
        self.assertEqual(agg["f1"]["severity"], "high")
        self.assertEqual(agg["f1"]["verdict"], "uphold")
        self.assertTrue(agg["f1"]["block"],
                        "HIGH uphold must block; reordering must not swap verdicts")
        self.assertEqual(agg["f2"]["severity"], "low")
        self.assertEqual(agg["f2"]["verdict"], "dismiss")
        self.assertFalse(agg["f2"]["block"])

    def test_orphan_id_falls_into_orphan_path_and_unmatched_finding_fails_safe(self):
        """AC: validator echoes an id that matches no Codex finding.
        - The parsed line goes to the existing `orphan-i` path.
        - The Codex finding whose verdict was never claimed falls into the
          existing "validator output missing" fail-safe.
        - The orphan entry never blocks regardless of its severity/verdict
          (refs claude-harness-work#28 — validator-scope contract).
        """
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
        ]
        validator_out = (
            "[HIGH] uphold id=zzz-unknown src/a.py:10 — src/a.py:10: bad\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        ids = [a["finding_id"] for a in vj["aggregate"]]
        self.assertIn("f1", ids, "Codex finding f1 must appear in aggregate")
        orphan = next((a for a in vj["aggregate"]
                       if a["finding_id"].startswith("orphan-")), None)
        self.assertIsNotNone(
            orphan, f"orphaned parsed line must produce orphan-i entry, got {ids}",
        )
        self.assertFalse(
            orphan["block"],
            "orphan never blocks regardless of severity/verdict (#28)",
        )
        f1 = next(a for a in vj["aggregate"] if a["finding_id"] == "f1")
        self.assertEqual(f1["verdict"], "unsure",
                         "unclaimed Codex finding must fail-safe to unsure")
        # critical/high + unsure must block per ADR-0005 verdict table
        self.assertTrue(f1["block"])

    def test_sanity_mismatch_on_file_demotes_to_unsure(self):
        """AC: validator echoes the right id but a divergent `file`.
        That finding's verdict is demoted to `unsure`; an stderr warning
        names the id and the divergent field.
        """
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
        ]
        validator_out = (
            "[HIGH] uphold id=f1 src/WRONG.py:10 — src/a.py:10: bad\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        f1 = next(a for a in vj["aggregate"] if a["finding_id"] == "f1")
        self.assertEqual(f1["verdict"], "unsure",
                         "sanity mismatch on file must demote verdict to unsure")
        self.assertTrue(f1["block"],
                        "high + unsure still blocks (ADR-0005 verdict table)")
        self.assertIn("f1", r.stderr,
                      f"stderr must name the id; got: {r.stderr!r}")
        self.assertIn("file", r.stderr,
                      f"stderr must name the divergent field; got: {r.stderr!r}")

    def test_sanity_mismatch_on_line_demotes_to_unsure(self):
        """Coverage for the line component of the sanity check."""
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
        ]
        validator_out = (
            "[HIGH] uphold id=f1 src/a.py:99 — src/a.py:10: bad\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        f1 = next(a for a in vj["aggregate"] if a["finding_id"] == "f1")
        self.assertEqual(f1["verdict"], "unsure")
        self.assertIn("f1", r.stderr)
        self.assertIn("line", r.stderr)

    def test_sanity_mismatch_on_severity_demotes_to_unsure(self):
        """Coverage for the severity component of the sanity check."""
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
        ]
        # Validator says LOW; Codex says high → severity divergence.
        validator_out = (
            "[LOW] uphold id=f1 src/a.py:10 — src/a.py:10: bad\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        f1 = next(a for a in vj["aggregate"] if a["finding_id"] == "f1")
        self.assertEqual(f1["verdict"], "unsure")
        self.assertIn("f1", r.stderr)
        self.assertIn("severity", r.stderr)

    def test_duplicate_id_demotes_to_unsure(self):
        """AC: validator emits two lines with the same id.
        That finding becomes `unsure` + stderr warning names the id and
        the count. Other findings remain unaffected.
        """
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
            {"id": "f2", "severity": "high", "file": "src/b.py", "line_start": 20,
             "title": "bad", "body": "..."},
        ]
        validator_out = (
            "[HIGH] uphold id=f1 src/a.py:10 — src/a.py:10: bad\n"
            "[HIGH] dismiss id=f1 src/b.py:20 — src/b.py:20: also f1?\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        agg = {a["finding_id"]: a for a in vj["aggregate"]}
        self.assertEqual(agg["f1"]["verdict"], "unsure",
                         "duplicate id must demote f1 to unsure")
        self.assertIn("f1", r.stderr)
        self.assertIn("2", r.stderr,
                      f"stderr must name the count; got: {r.stderr!r}")
        # f2 had no validator verdict → existing fail-safe (also unsure)
        self.assertEqual(agg["f2"]["verdict"], "unsure")

    def test_duplicate_codex_id_does_not_reuse_one_verdict(self):
        """AC (#29): two distinct Codex findings share id="dup", both HIGH,
        and the validator emits a SINGLE `[HIGH] dismiss id=dup` line.
        Without the Codex-side guard, both findings would reuse that one
        `dismiss` verdict → block:false → hard-mode fail-open. Both must
        instead fail-safe to unsure + block; stderr names the id.
        """
        findings = [
            {"id": "dup", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
            {"id": "dup", "severity": "high", "file": "src/b.py", "line_start": 20,
             "title": "also bad", "body": "..."},
        ]
        validator_out = (
            "[HIGH] dismiss id=dup src/a.py:10 — docs/adr/0001.md:3: allowed\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        dup_entries = [a for a in vj["aggregate"] if a["finding_id"] == "dup"]
        self.assertEqual(len(dup_entries), 2,
                         f"both colliding findings must produce entries, got {vj['aggregate']}")
        for a in dup_entries:
            self.assertEqual(a["verdict"], "unsure",
                             "colliding Codex id must fail-safe to unsure, not reuse dismiss")
            self.assertTrue(a["block"],
                            "HIGH unsure must block — no dismiss may be smuggled through")
        self.assertIn("dup", r.stderr)
        self.assertIn("2", r.stderr,
                      f"stderr must name the collision count; got: {r.stderr!r}")

    def test_duplicate_codex_id_coexists_with_duplicate_validator_lines(self):
        """AC (#29): two findings share id="dup" AND the validator emits TWO
        `[HIGH] dismiss id=dup` lines — exercising the new Codex-side guard
        and the existing validator-side duplicate guard at once. The
        Codex-side guard fires first, so both findings fail-safe to unsure +
        block; no `block:false` is smuggled through, and the two parsed
        lines are consumed (no spurious orphan entries).
        """
        findings = [
            {"id": "dup", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
            {"id": "dup", "severity": "high", "file": "src/b.py", "line_start": 20,
             "title": "also bad", "body": "..."},
        ]
        validator_out = (
            "[HIGH] dismiss id=dup src/a.py:10 — docs/adr/0001.md:3: allowed\n"
            "[HIGH] dismiss id=dup src/b.py:20 — docs/adr/0001.md:9: allowed\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        dup_entries = [a for a in vj["aggregate"] if a["finding_id"] == "dup"]
        self.assertEqual(len(dup_entries), 2)
        for a in dup_entries:
            self.assertEqual(a["verdict"], "unsure")
            self.assertTrue(a["block"])
        orphans = [a for a in vj["aggregate"] if a["finding_id"].startswith("orphan-")]
        self.assertEqual(orphans, [],
                         f"parsed lines for a colliding id must not become orphans, got {orphans}")
        self.assertIn("dup", r.stderr)

    def test_synthesized_id_when_codex_omits_id(self):
        """AC: Codex emits a finding without an `id`.
        cmd_build_input synthesizes `finding-{i}`, the validator echoes
        that synthesized id, and the aggregator pairs correctly.
        """
        # Step 1: build-input synthesizes finding-0 for an id-less finding.
        codex_findings = [
            {"severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
        ]
        codex_json = write_tmp_json(
            self.tmpdir,
            "codex.json",
            {"result": {"findings": codex_findings}, "codex": {"status": "ok"}},
        )
        payload = run_build_input(codex_json)
        self.assertEqual(
            payload["codex_json"]["findings"][0]["id"], "finding-0",
            "cmd_build_input must synthesize id=finding-0 when Codex omits id",
        )
        # Step 2: validator echoes the synthesized id; pairing must succeed.
        validator_out = (
            "[HIGH] uphold id=finding-0 src/a.py:10 — src/a.py:10: bad\n"
        )
        validator_path = self.tmpdir / "validator.out"
        validator_path.write_text(validator_out, encoding="utf-8")
        out_dir = self.tmpdir / "out"
        r = subprocess.run(
            ["python3", str(AGGREGATE), "write-outputs",
             "--codex-json", str(codex_json),
             "--validator-output", str(validator_path),
             "--soft-mode", "false",
             "--out-dir", str(out_dir)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        vj = json.loads((out_dir / "validators.json").read_text())
        self.assertEqual(len(vj["aggregate"]), 1)
        a = vj["aggregate"][0]
        self.assertEqual(a["finding_id"], "finding-0")
        self.assertEqual(a["verdict"], "uphold")
        self.assertTrue(a["block"])

    def test_missing_validator_line_preserves_22_failsafe(self):
        """AC: validator emits fewer parsed lines than findings.
        The unmatched finding falls into the existing "validator output
        missing" fail-safe (no regression from claude-harness-work#22).
        """
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
            {"id": "f2", "severity": "high", "file": "src/b.py", "line_start": 20,
             "title": "bad", "body": "..."},
        ]
        # Only f1 echoed; f2 silent.
        validator_out = (
            "[HIGH] uphold id=f1 src/a.py:10 — src/a.py:10: bad\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        agg = {a["finding_id"]: a for a in vj["aggregate"]}
        self.assertEqual(agg["f1"]["verdict"], "uphold")
        self.assertTrue(agg["f1"]["block"])
        self.assertEqual(agg["f2"]["verdict"], "unsure",
                         "missing validator verdict → fail-safe unsure (#22)")
        self.assertTrue(agg["f2"]["block"],
                        "high + unsure still blocks (ADR-0005 verdict table)")

    def test_orphan_high_uphold_does_not_block(self):
        """AC (claude-harness-work#28): zero Codex findings + a single
        hallucinated `[HIGH] uphold` validator line must not produce a
        blocker. The orphan entry is recorded for audit (severity/verdict
        preserved) but `block` is forced false — the validator's scope
        contract is `classify, not author`.
        """
        findings: list[dict] = []
        validator_out = (
            "[HIGH] uphold id=made-up src/a.py:1 — src/a.py:1: invented\n"
        )
        r, vj = self._run_write_outputs(findings, validator_out)
        self.assertEqual(len(vj["aggregate"]), 1,
                         f"expected 1 orphan entry; got {vj['aggregate']!r}")
        orphan = vj["aggregate"][0]
        self.assertEqual(orphan["finding_id"], "orphan-0")
        self.assertEqual(orphan["severity"], "high",
                         "validator-supplied severity is preserved for audit")
        self.assertEqual(orphan["verdict"], "uphold",
                         "validator-supplied verdict is preserved for audit")
        self.assertFalse(
            orphan["block"],
            "[HIGH] uphold orphan must NOT block — scope contract (#28)",
        )
        self.assertIn("made-up", r.stderr,
                      f"stderr must name the orphan id; got: {r.stderr!r}")

    def test_orphan_alongside_valid_finding(self):
        """AC (claude-harness-work#28): one valid Codex finding + the
        validator emits both a correct line AND a hallucinated `[HIGH]
        uphold` orphan. The valid pair is decided normally (block=true);
        the orphan is recorded with block=false.
        """
        findings = [
            {"id": "f1", "severity": "high", "file": "src/a.py", "line_start": 10,
             "title": "bad", "body": "..."},
        ]
        validator_out = (
            "[HIGH] uphold id=f1 src/a.py:10 — src/a.py:10: real\n"
            "[HIGH] uphold id=made-up src/b.py:1 — src/b.py:1: invented\n"
        )
        _, vj = self._run_write_outputs(findings, validator_out)
        self.assertEqual(len(vj["aggregate"]), 2,
                         f"expected 2 entries (valid + orphan); got {vj['aggregate']!r}")
        agg = {a["finding_id"]: a for a in vj["aggregate"]}
        self.assertIn("f1", agg)
        self.assertEqual(agg["f1"]["verdict"], "uphold")
        self.assertTrue(agg["f1"]["block"],
                        "valid HIGH uphold still blocks via decide_block")
        orphan_id = next(fid for fid in agg if fid.startswith("orphan-"))
        self.assertEqual(agg[orphan_id]["verdict"], "uphold")
        self.assertFalse(agg[orphan_id]["block"],
                         "orphan never blocks (#28)")

    def test_orphan_preserves_severity_verdict_in_audit(self):
        """Regression: existing orphan paths whose severity is below the
        blocking floor (e.g. `[MEDIUM] uphold`) still preserve their
        validator-supplied severity + verdict in `validators.json`. Only
        `block` is forced False; the audit trail is unchanged.
        """
        findings: list[dict] = []
        validator_out = (
            "[MEDIUM] uphold id=med-orphan src/a.py:1 — src/a.py:1: moderate\n"
        )
        _, vj = self._run_write_outputs(findings, validator_out)
        self.assertEqual(len(vj["aggregate"]), 1)
        orphan = vj["aggregate"][0]
        self.assertEqual(orphan["severity"], "medium",
                         "audit trail: validator's severity must survive")
        self.assertEqual(orphan["verdict"], "uphold",
                         "audit trail: validator's verdict must survive")
        self.assertFalse(orphan["block"],
                         "block=False regardless of pre-#28 behaviour")


if __name__ == "__main__":
    if not AGGREGATE.exists():
        raise SystemExit(f"aggregate.py not found at {AGGREGATE}")
    if shutil.which("jq") is None:
        raise SystemExit("`jq` not on $PATH — install jq to run these tests.")
    unittest.main(verbosity=2)
