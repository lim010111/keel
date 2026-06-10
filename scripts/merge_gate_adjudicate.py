#!/usr/bin/env python3
"""merge_gate_adjudicate.py — finding-first BLIND adjudication UX (#31 Phase 1).

The verdict-capture tool that fills the blank human columns in the canonical
measurement ledger (`.scratch/merge-gate-measurement/log.md`). It is the
load-bearing instrument for false-positive detection (design §8): a false
positive is `verdict(gate)=block ∧ human:should-block?=no`, which is only
recoverable once the human column is filled.

The defining property is **blindness**: for each pending finding the tool shows
the finding + its validator citation BEFORE revealing the gate's mechanical
`block`/`verdict`, so "would I have blocked this?" is not anchored to what the
gate actually did (design §3, §8). The blinding lives in the capture UX, not the
file layout — the stored row keeps both the machine decision and the human one.

This tool **only reads + rewrites the ledger markdown** — it never runs the gate,
never re-derives freshness, and never touches `merge_gate_local.py` (ADR-0009
instrument-around). The schema it parses/writes is design §3, already produced by
`merge_gate_measure.py` (`build_row` / `_findings_table` / `_citation_block`).

Architecture: pure functions (parse / view / apply) do all the work; the
interactive shell `adjudicate(text, ask, show)` takes INJECTED `ask`/`show`
callables so the loop is unit-testable without real stdin. `main` is thin — it
wires real `input()`/`print()` into `adjudicate`.

Stdlib only. Run tests:  python3 scripts/test_merge_gate_adjudicate.py -v
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# The canonical ledger (design §1). `main` defaults --ledger here; tests assert
# the wiring but NEVER auto-run against it.
DEFAULT_LEDGER = Path("/home/shine/.claude/.scratch/merge-gate-measurement/log.md")


def _scrub_input(s: str) -> str:
    """Sanitise one captured verdict token before it can reach the ledger. The
    human verdicts are plain tokens (yes/no/would-pass/=N), but `input()` decodes
    stdin with the locale codec — under a non-UTF-8 locale (or a stray pasted byte)
    it returns LONE SURROGATES via surrogateescape. A surrogate is not UTF-8
    encodable, so writing it back blows up the whole ledger write (the crash that
    lost a hand-entered adjudication run). Drop lone surrogates + control chars
    (keep tab); legitimate printable Unicode is preserved."""
    return "".join(
        c for c in s
        if not (0xD800 <= ord(c) <= 0xDFFF) and (c >= " " or c == "\t")
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` ATOMICALLY: a temp file in the same dir + os.replace,
    so a failure (encode error, disk-full, crash) leaves the ORIGINAL file fully
    intact — NEVER a truncated/empty ledger. Path.write_text opens 'w' (truncate)
    then encodes+writes, so any mid-write failure corrupts the canonical record
    in place — exactly the 'everything gets deleted' risk this replaces. Mirrors
    merge_gate_local.write_summary_atomic. Strict UTF-8: the ledger stays valid
    UTF-8 (so the next read never fails); if some surrogate still reaches `text`
    the encode raises HERE, on the temp file, and the original is untouched."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

# A blank human cell renders as ⬜ (build_row); an empty same-as cell is "".
_BLANK = {"", "⬜", "—"}

# Push-section header (design §3). Trailing text (e.g. the calibration suffix the
# real ledger appends) is tolerated and ignored.
_PUSH_RE = re.compile(
    r"^## push (?P<id>\S+) · (?P<when>.+?) · (?P<base>\S+)\.\.(?P<diff>\S+?)(?:\s+.*)?$"
)
# A findings-table data row: 12 pipe-delimited cells (design §3 _findings_table).
# Header/separator rows have a non-integer first cell, so they don't match.
# `\s*` around the index tolerates COLUMN-ALIGNED padding (`| 0   |`): the canonical
# build_row emits single-space cells, but a markdown table formatter run over the
# committed ledger pads them, and the parser must still read those rows (#35 — else
# advisory_signal_stats undercounts forced-unsure on the real ledger). _split_finding_cells
# strips each cell, so padding is harmless once the row matches.
_FINDING_RE = re.compile(r"^\|\s*(?P<idx>\d+)\s*\|(?P<rest>.*)\|$")
# A citation sub-block entry: "  - [<#>] <fid> · <verdict> · <text>".
_CITATION_RE = re.compile(r"^\s*- \[(?P<idx>\d+)\] (?P<fid>\S+) · (?P<verdict>\S+) · (?P<text>.*)$")


class Finding:
    """One findings-table row + its joined citation (design §3)."""

    __slots__ = (
        "index", "location", "reviewers", "severity", "validator_verdict",
        "block", "conc", "conf", "rconf", "human_real", "human_should_block",
        "same_as", "citation", "_line_no",
    )

    def __init__(self, index, location, reviewers, severity, validator_verdict,
                 block, conc, conf, rconf, human_real, human_should_block,
                 same_as, citation, line_no):
        self.index = index
        self.location = location
        self.reviewers = reviewers
        self.severity = severity
        self.validator_verdict = validator_verdict
        self.block = block
        self.conc = conc
        self.conf = conf
        self.rconf = rconf
        self.human_real = human_real
        self.human_should_block = human_should_block
        self.same_as = same_as
        self.citation = citation
        self._line_no = line_no  # 0-based index into the ledger's line list

    @property
    def adjudicated(self) -> bool:
        """A finding is adjudicated once its human cells are no longer blank."""
        return self.human_real not in _BLANK or self.human_should_block not in _BLANK

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Finding(index={self.index}, location={self.location!r}, block={self.block})"


class Section:
    """One push section (design §3)."""

    __slots__ = (
        "push_id", "when", "base", "diff", "reviewers", "verdict", "block_count",
        "freshness", "intervention", "push_verdict", "advisory", "findings",
        "_header_line_no", "_push_verdict_line_no",
    )

    def __init__(self, push_id, when, base, diff, header_line_no):
        self.push_id = push_id
        self.when = when
        self.base = base
        self.diff = diff
        self.reviewers = ""
        self.verdict = "—"
        self.block_count = None
        self.freshness = ""
        self.intervention = ""
        self.push_verdict = "—"
        # #35 — per-reviewer soft-steer conformance ("<rev>=<status>; …"), set from
        # the "- advisory conformance ⓓ:" line build_row emits when a reviewer
        # produced no usable signal; "" when all reviewers were ok.
        self.advisory = ""
        self.findings: list[Finding] = []
        self._header_line_no = header_line_no
        self._push_verdict_line_no = None


class Ledger(list):
    """List of Sections with a couple of lookup conveniences."""

    def section(self, push_id: str) -> Section:
        for s in self:
            if s.push_id == push_id:
                return s
        raise KeyError(push_id)

    def findings_for(self, push_id: str) -> list[Finding]:
        return self.section(push_id).findings


# --------------------------------------------------------------------------
# 1. parse_ledger
# --------------------------------------------------------------------------
def _split_finding_cells(line: str) -> list[str] | None:
    """Split a 12-cell findings row into its inner cells. Returns None if the line
    is not a data row (header/separator/non-finding)."""
    m = _FINDING_RE.match(line)
    if not m:
        return None
    # Reconstruct full cell list incl. the leading index cell.
    cells = [c.strip() for c in line.split("|")[1:-1]]
    return cells if len(cells) == 12 else None


def _to_int(s: str):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_ledger(text: str) -> Ledger:
    """Parse the ledger markdown into push sections, each exposing its machine
    fields and a `findings[]` list (with the validator citation joined in by
    finding index + fid). Non-push prose / templates / fences are ignored."""
    lines = text.splitlines()
    ledger = Ledger()
    cur: Section | None = None
    # Pending citations for the current section, keyed by finding index.
    cur_citations: dict[int, str] = {}

    def finalize(section: Section, citations: dict[int, str]) -> None:
        for f in section.findings:
            # Join by index (the sub-block is keyed by index AND fid; index is the
            # table join key — design §3). Only adopt if the fid also matches when
            # present, but index is authoritative for the row.
            if f.index in citations:
                f.citation = citations[f.index]

    in_fence = False
    for i, line in enumerate(lines):
        # Skip fenced code blocks (the ledger's per-push *template* lives in one —
        # it must not be mistaken for a real push section).
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        m = _PUSH_RE.match(line)
        if m:
            if cur is not None:
                finalize(cur, cur_citations)
                ledger.append(cur)
            cur = Section(m.group("id"), m.group("when").strip(),
                          m.group("base"), m.group("diff"), i)
            cur_citations = {}
            continue
        if cur is None:
            continue

        cm = _CITATION_RE.match(line)
        if cm:
            cur_citations[int(cm.group("idx"))] = cm.group("text").strip()
            continue

        if line.startswith("- reviewers:"):
            # "- reviewers: codex, claude        verdict(gate): block · block_count=1"
            rev_part, _, verdict_part = line.partition("verdict(gate):")
            cur.reviewers = rev_part[len("- reviewers:"):].strip()
            vp = verdict_part.strip()
            if "·" in vp:
                v, _, bc = vp.partition("·")
                cur.verdict = v.strip()
                cur.block_count = _to_int(bc.strip().replace("block_count=", ""))
            elif vp:
                cur.verdict = vp.strip()
            continue
        if line.startswith("- freshness ⓐ:"):
            # "- freshness ⓐ: failing-findings   intervention: no   [flagged: ...]"
            body = line[len("- freshness ⓐ:"):]
            state_part, _, interv_part = body.partition("intervention:")
            cur.freshness = state_part.strip()
            interv = interv_part.strip()
            # Strip a trailing "[flagged: ...]" off the intervention token.
            if "[" in interv:
                interv = interv[:interv.index("[")].strip()
            cur.intervention = interv.split()[0] if interv else ""
            continue
        if line.startswith("- human push-verdict:"):
            body = line[len("- human push-verdict:"):]
            # Drop the trailing "   ← captured finding-first" marker.
            body = body.split("←")[0].strip()
            cur.push_verdict = body if body else "—"
            cur._push_verdict_line_no = i
            continue
        if line.startswith("- advisory conformance ⓓ:"):
            # #35 — the per-reviewer soft-steer degradation (e.g. "claude=malformed-
            # payload"); drop the trailing "(no usable signal …)" parenthetical.
            body = line[len("- advisory conformance ⓓ:"):].strip()
            cur.advisory = body.split("(")[0].strip()
            continue

        cells = _split_finding_cells(line)
        if cells is not None:
            cur.findings.append(Finding(
                index=int(cells[0]),
                location=cells[1],
                reviewers=cells[2],
                severity=cells[3],
                validator_verdict=cells[4],
                block=(cells[5] == "✓"),
                conc=cells[6],
                conf=cells[7],
                rconf=cells[8],
                human_real=cells[9],
                human_should_block=cells[10],
                same_as=cells[11],
                citation="",
                line_no=i,
            ))

    if cur is not None:
        finalize(cur, cur_citations)
        ledger.append(cur)
    return ledger


# --------------------------------------------------------------------------
# 2. blind_view — the blinding invariant
# --------------------------------------------------------------------------
def blind_view(f: Finding) -> tuple[str, str]:
    """Return (pre_reveal, post_reveal).

    pre_reveal: the finding's location, reviewers and its validator CITATION —
    but NOT severity, NOT the validator verdict, NOT the gate block flag. This is
    what the operator judges blind. Severity and the validator verdict are held
    back because the gate block decision is DETERMINISTIC in them —
    block = (severity ∈ {critical,high}) ∧ (verdict ∈ {uphold,unsure}) — so
    exposing the pair pre-verdict would let the operator infer the block decision
    and anchor "would I have blocked this?" to it, defeating the blinding.
    post_reveal: the gate's mechanical decision (block flag) together with the
    severity + validator verdict that produced it, revealed only AFTER the human
    verdict is captured.

    The blinding invariant is tested in both directions: leaking block/verdict —
    or severity/validator — into pre_reveal must fail the suite (design §3, §8)."""
    pre_lines = [
        f"finding [{f.index}]  {f.location}",
        f"  reviewers: {f.reviewers}",
    ]
    if f.citation:
        pre_lines.append(f"  citation: {f.citation}")
    else:
        pre_lines.append("  citation: (none snapshotted)")
    pre = "\n".join(pre_lines)

    gate = "✓ blocked" if f.block else "✗ not blocked"
    post = f"  gate decision: {gate}   severity: {f.severity}   validator: {f.validator_verdict}"
    return pre, post


# --------------------------------------------------------------------------
# 3. pending_findings
# --------------------------------------------------------------------------
def pending_findings(sections) -> list[tuple[str, Finding]]:
    """(push_id, finding) for every finding whose human cells are still blank —
    already-adjudicated rows are skipped."""
    out: list[tuple[str, Finding]] = []
    for s in sections:
        for f in s.findings:
            if not f.adjudicated:
                out.append((s.push_id, f))
    return out


# --------------------------------------------------------------------------
# 4/5. surgical write-back (apply_finding_verdict / apply_push_verdict)
# --------------------------------------------------------------------------
def _render_finding_row(f: Finding, human_real: str, human_should_block: str,
                        same_as: str) -> str:
    """Rebuild one findings-table row with new human cells, every machine cell
    byte-identical to what build_row emitted (design §3 _findings_table)."""
    block = "✓" if f.block else "✗"
    real = human_real if human_real else "⬜"
    sb = human_should_block if human_should_block else "⬜"
    sa = same_as  # empty stays empty (renders as "  |", matching the wrapper)
    return (
        f"| {f.index} | {f.location} | {f.reviewers} | {f.severity} | "
        f"{f.validator_verdict} | {block} | {f.conc} | {f.conf} | {f.rconf} | "
        f"{real} | {sb} | {sa} |"
    )


def apply_finding_verdict(text: str, push_id: str, index: int, real: str,
                          should_block: str, same_as: str) -> str:
    """Set ONLY that one finding row's human:real? / human:should-block? / same-as
    cells. Every other byte of the ledger is unchanged; applying the same verdict
    twice is idempotent."""
    sections = parse_ledger(text)
    try:
        section = sections.section(push_id)
    except KeyError:
        return text
    target = next((f for f in section.findings if f.index == index), None)
    if target is None:
        return text

    lines = text.splitlines(keepends=True)
    old = lines[target._line_no]
    # Preserve the line's original trailing newline (keepends).
    nl = "\n" if old.endswith("\n") else ""
    new = _render_finding_row(target, real, should_block, same_as) + nl
    if new == old:
        return text  # idempotent — nothing to write
    lines[target._line_no] = new
    return "".join(lines)


def apply_push_verdict(text: str, push_id: str, verdict: str) -> str:
    """Set that section's "human push-verdict" line (would-block | would-pass),
    preserving the "  ← captured finding-first" marker. Idempotent."""
    sections = parse_ledger(text)
    try:
        section = sections.section(push_id)
    except KeyError:
        return text
    if section._push_verdict_line_no is None:
        return text

    lines = text.splitlines(keepends=True)
    ln = section._push_verdict_line_no
    old = lines[ln]
    nl = "\n" if old.endswith("\n") else ""
    new = f"- human push-verdict: {verdict}   ← captured finding-first" + nl
    if new == old:
        return text
    lines[ln] = new
    return "".join(lines)


# --------------------------------------------------------------------------
# 6. false-positive detection (design §8 handshake)
# --------------------------------------------------------------------------
def is_false_positive(f: Finding) -> bool:
    """A false positive: the gate blocked on this finding (block=true) but the
    operator, judging finding-first, says it should NOT have blocked
    (human:should-block?=no). Recoverable purely from the written row."""
    return f.block and f.human_should_block.strip().lower() == "no"


def false_positives(sections) -> list[tuple[str, Finding]]:
    """(push_id, finding) for every confirmed false positive in the ledger."""
    out: list[tuple[str, Finding]] = []
    for s in sections:
        for f in s.findings:
            if is_false_positive(f):
                out.append((s.push_id, f))
    return out


# --------------------------------------------------------------------------
# #35 — advisory-path conformance recurrence (A: accept + measure)
# --------------------------------------------------------------------------
# The validator-side duplicate-id guard's citation (aggregate.py): a finding it
# forced to fail-safe `unsure` carries this exact phrase. DISTINCT from the #29
# Codex-side guard ("duplicate Codex finding id"), which #35 explicitly excludes.
_FORCED_UNSURE_PHRASE = "duplicate id in validator output"


def is_forced_unsure(f: Finding) -> bool:
    """True iff this finding's `unsure` verdict was FORCED by the validator-side
    duplicate-id guard (a degraded signal), not a genuine validator 'unsure'.
    Recoverable from the preserved citation (#35 AC2)."""
    return (f.validator_verdict.strip().lower() == "unsure"
            and _FORCED_UNSURE_PHRASE in (f.citation or ""))


def advisory_signal_stats(sections) -> dict:
    """#35 (A) — the advisory-path degradation rate over the ledger's organic rows,
    so soft-steer noise is tracked and not silently read as reviewer agreement. An
    'organic row' is a push that actually ran a produce (verdict != '—'). Returns
    counts + the offending push/finding ids for both soft-steer modes:
      · reviewer 'no usable signal' — a non-`ok` reviewer codex_status (the claude
        reviewer→prose→malformed-payload case, surfaced by build_row's advisory line);
      · validator 'forced-unsure' — a finding the validator dup-id guard demoted.
    n today is tiny (#35: n=1); this makes the rate computable as N grows so the
    operator can judge whether (A) holds or a mitigation (B/C/D) is warranted."""
    rows = [s for s in sections if s.verdict not in ("—", "")]
    reviewer_no_signal = [s for s in rows if s.advisory]
    # Both rates are over ORGANIC rows only (`rows`, not all `sections`): a forced-
    # unsure can only arise from a row that actually produced + validated, so a
    # non-produced ('—') row never contributes (AC3 — rate over organic N).
    forced_by_row = [(s, [f for f in s.findings if is_forced_unsure(f)]) for s in rows]
    forced_rows = [s for s, ff in forced_by_row if ff]
    forced = [(s.push_id, f) for s, ff in forced_by_row for f in ff]
    return {
        "organic_rows": len(rows),
        "reviewer_no_signal_rows": len(reviewer_no_signal),
        "reviewer_no_signal": [(s.push_id, s.advisory) for s in reviewer_no_signal],
        # AC3's unit is "dup-id ROWS"; the per-finding count is the granular detail.
        "forced_unsure_rows": len(forced_rows),
        "forced_unsure_findings": len(forced),
        "forced_unsure": [(pid, f.index, f.location) for pid, f in forced],
    }


def _format_advisory_stats(stats: dict) -> str:
    """Human-readable advisory-stats report (the `advisory-stats` subcommand)."""
    n = stats["organic_rows"]
    over = f" / {n}" if n else ""
    lines = [
        "merge-gate advisory-path conformance (#35 — A: accept + measure)",
        f"  organic rows (produced):          {n}",
        f"  reviewer no-usable-signal rows:   {stats['reviewer_no_signal_rows']}{over}",
        f"  validator forced-unsure rows:     {stats['forced_unsure_rows']}{over}"
        + f"  ({stats['forced_unsure_findings']} finding(s))",
    ]
    for pid, detail in stats["reviewer_no_signal"]:
        lines.append(f"    · reviewer no-signal: push {pid} — {detail}")
    for pid, idx, loc in stats["forced_unsure"]:
        lines.append(f"    · forced-unsure: push {pid} #{idx} — {loc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The thin interactive shell — ask/show INJECTED for testability.
# --------------------------------------------------------------------------
def adjudicate(text: str, ask, show) -> str:
    """Finding-first blind adjudication loop. For each pending finding:
      1. show(pre_reveal)           — finding + citation, blind to the gate
      2. ask real? / should-block? / same-as
      3. write back via apply_finding_verdict
      4. show(post_reveal)          — reveal the gate decision AFTER capture
    Then, for each section that has a pending push-verdict, capture it.

    `ask(prompt) -> str` and `show(text) -> None` are injected so the loop is
    unit-testable without real stdin. Returns the new ledger text."""
    sections = parse_ledger(text)
    pending = pending_findings(sections)

    touched_pushes: list[str] = []
    for push_id, f in pending:
        pre, post = blind_view(f)
        show(pre)
        real = ask(f"  [{push_id} #{f.index}] human:real? (was this a real issue?) ").strip()
        should_block = ask(f"  [{push_id} #{f.index}] human:should-block? (would you have blocked?) ").strip()
        same_as = ask(f"  [{push_id} #{f.index}] same-as (e.g. =0, or blank) ").strip()
        text = apply_finding_verdict(text, push_id, f.index, real, should_block, same_as)
        # Reveal the gate's mechanical decision only AFTER the human verdict.
        show(post)
        if push_id not in touched_pushes:
            touched_pushes.append(push_id)

    # Push-verdict: ask for any section whose push-verdict is still blank, but only
    # those that had at least one finding adjudicated this run (the operator just
    # judged the push). Re-parse so the cells we just wrote are visible.
    sections = parse_ledger(text)
    for s in sections:
        if s.push_id in touched_pushes and s.push_verdict in _BLANK:
            v = ask(f"  [{s.push_id}] push-verdict? (would-block | would-pass) ").strip()
            if v:
                text = apply_push_verdict(text, s.push_id, v)

    return text


# --------------------------------------------------------------------------
# 7. main(argv) — thin CLI (real input()/print() wired into adjudicate).
# --------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="merge-gate-adjudicate",
        description="finding-first BLIND adjudication of the measurement ledger (#31)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("adjudicate", help="fill the blank human verdict columns, finding-first")
    pa.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                    help=f"path to the measurement ledger (default: {DEFAULT_LEDGER})")
    ps = sub.add_parser("advisory-stats",
                        help="report the #35 advisory-path conformance rate (read-only)")
    ps.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                    help=f"path to the measurement ledger (default: {DEFAULT_LEDGER})")
    return p


def main(argv=None, ask=None, show=None) -> int:
    """Thin CLI. `ask`/`show` default to real input()/print(); tests inject fakes."""
    ns = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if ask is None:
        ask = input
    if show is None:
        def show(t):  # noqa: E306 - tiny local
            print(t)
    # Scrub every captured token (surrogates/control chars) at the single capture
    # chokepoint, so a non-UTF-8 terminal can never poison the ledger encoding.
    _raw_ask = ask

    def ask(prompt):  # noqa: E306
        return _scrub_input(_raw_ask(prompt))

    if ns.cmd == "adjudicate":
        text = ns.ledger.read_text(encoding="utf-8")
        out = adjudicate(text, ask, show)
        if out != text:
            _atomic_write_text(ns.ledger, out)
            show(f"\nmerge-gate-adjudicate: wrote {ns.ledger}")
        else:
            show("merge-gate-adjudicate: nothing pending — ledger unchanged")
        return 0
    if ns.cmd == "advisory-stats":
        # Read-only (#35 A): never mutates the ledger — just reports the rate.
        text = ns.ledger.read_text(encoding="utf-8")
        show(_format_advisory_stats(advisory_signal_stats(parse_ledger(text))))
        return 0
    return 0  # pragma: no cover - argparse `required` precludes this


if __name__ == "__main__":
    sys.exit(main())
