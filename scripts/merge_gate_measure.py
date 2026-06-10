#!/usr/bin/env python3
"""merge_gate_measure.py — the local-profile measurement capture wrapper (#31).

Phase 1 of #31. This wrapper **instruments around** the shipped #30 gate: it
never modifies `merge_gate_local.py` and never changes a gate decision (ADR-0009
§ keep-the-stable-gate-stable). Its only job is to durably capture, at push time,
the freshness/verdict/latency signals the gate computes but does **not** persist,
into the per-repo canonical ledger `.scratch/merge-gate-measurement/log.md`.

The seam is three read points (design §2 — `measurement-phase0-design.md`),
because a single shim cannot see the post-commit producer's auto-produces
(`launch_produce` hardcodes the wrapper path and ignores `MERGE_GATE_WRAPPER`):

  ⓐ verify-wrap (load-bearing) — `pre-push.sh` calls this via `MERGE_GATE_WRAPPER`.
     It execs the real `merge_gate_local.py verify <args…>`, tees stdout/stderr
     byte-for-byte, recovers `state` by matching the real verify **stdout** against
     a pinned set of #30 markers (the advisory `verify` exits 0 for every state,
     so the exit code carries no freshness signal), snapshots the consumed
     `summary.json`, appends a ledger row, and exits with verify's own code.
     `state` recovery is stdout-parse ONLY — never a second `freshness_state()`
     oracle (that would reintroduce the #10 two-ledger-divergence class).

  ⓑ producer state-dir reader (robust, context-only) — counts auto-produces from
     the post-commit producer's own durable state: the `merge-gate produce:
     verdict=` lines in `produce.log` (volume) + `state.json` (last_produce_ts),
     written by `merge_gate_post_commit.py launch_produce` (#33/ADR-0014 — this
     replaced the retired Stop scheduler), never a shim.

  ⓒ produce-shim (best-effort, context-only) — `merge_gate_measure.py produce …`
     records `--force`/`--intent` (not in `summary.json`; the auto-producer never
     passes them ⇒ force ⇒ manual by construction) into `produce-intents.jsonl`,
     then execs the real produce.

Stdlib only. Run tests:  python3 scripts/test_merge_gate_measure.py -v
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
# The real #30 gate this wrapper instruments around. Overridable for tests.
REAL_WRAPPER = Path(os.environ.get("MERGE_GATE_MEASURE_REAL",
                                   str(SCRIPTS / "merge_gate_local.py")))

# merge_gate_local is imported as a LIBRARY only to *locate* the consumed
# artefact (resolve_base_sha + canonical_diff + tuple_dir) — deterministic file
# location, NOT a freshness judgement. `state` still comes from stdout. Guarded:
# if the import fails (e.g. a fake real-wrapper in tests) the snapshot degrades to
# empty and the load-bearing state/row still gets written.
sys.path.insert(0, str(SCRIPTS))
try:
    import merge_gate_local as mg  # noqa: E402
except Exception:  # pragma: no cover - defensive; snapshot just degrades
    mg = None


# --------------------------------------------------------------------------
# ⓐ — the pinned #30 verify-stdout grammar.
# --------------------------------------------------------------------------
# (substring uniquely identifying the verify decision line, state,
#  intervention?, flagged?). Pinned to the EXACT wording printed by
# merge_gate_local.cmd_verify — an accepted, measurement-only coupling. Ordered:
# the post-promotion BLOCK/BYPASSED line prefixes are checked first so an advisory
# "review BLOCKED N finding(s)" detail is not mistaken for a client-side block.
#
# Intervention model (design §2ⓐ): the load-bearing burden headline is
# state ∈ HEADLINE_INTERVENTION_STATES; the other intervention-class states are
# rarer/edge and carry `flagged` so the operator notices them. fresh/none and the
# legitimate failing-findings block are NOT interventions.
_MARKERS: list[tuple[str, str, bool, bool]] = [
    ("merge-gate BYPASSED:",                                     "bypassed",            False, True),
    ("merge-gate BLOCK:",                                        "blocked",             False, True),
    ("fresh passing review for",                                 "fresh",               False, False),
    ("no in-scope changes to gate",                              "none",                False, False),
    ("no review artefact for this base+diff",                   "missing",             True,  False),
    ("review scope changed since the artefact",                 "scope-mismatch",      True,  False),
    ("could not compute the review diff for the pushed range",  "unreviewable",        True,  False),
    ("reviewer(s) failed:",                                      "failing-incomplete",  True,  True),
    ("review BLOCKED",                                           "failing-findings",    False, False),
    ("artefact schema is incompatible",                          "schema-incompatible", True,  True),
    ("reviewer/validator tool version changed",                 "tool-drift",          True,  True),
    ("could not resolve base ref",                               "no-base",             True,  True),
]

# The clean, comparable burden headline (design §5). The §5 metric reads `state`
# membership in THIS set — the per-row `intervention` bool is a human-readable
# convenience; the state string is authoritative.
HEADLINE_INTERVENTION_STATES = frozenset({"missing", "scope-mismatch", "unreviewable"})

# #33 G1 — the bounded-verify-wait token (`cmd_verify` prints it after waiting on
# an in-flight produce). It is an ANNOTATION carried ALONGSIDE the decision line,
# not a freshness state, so it gets its own parse: the wrapper RECOVERS the wait
# duration from this token rather than timing the wait itself (which it cannot —
# the wait happens inside the real verify, across the wrapper seam). Pinned to the
# exact wording printed by merge_gate_local._await_pending_artefact.
_WAIT_TOKEN_RE = re.compile(r"merge-gate: waited (\d+)s for in-flight produce")


class VerifyState:
    """Parsed result of one verify invocation's stdout."""

    __slots__ = ("state", "intervention", "flagged", "raw_line", "wait_seconds")

    def __init__(self, state: str, intervention: bool, flagged: bool,
                 raw_line: str = "", wait_seconds: int = 0):
        self.state = state
        self.intervention = intervention
        self.flagged = flagged
        self.raw_line = raw_line
        self.wait_seconds = wait_seconds

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"VerifyState(state={self.state!r}, intervention={self.intervention}, "
                f"flagged={self.flagged}, wait_seconds={self.wait_seconds})")


def parse_wait_seconds(stdout: str) -> int:
    """Recover the bounded-verify-wait duration from verify's stdout (#33 G1), or
    0 when verify did not wait. PARSED, never recomputed — the wrapper must not be
    a second oracle for a value the gate already reported."""
    m = _WAIT_TOKEN_RE.search(stdout)
    return int(m.group(1)) if m else 0


def parse_verify_state(stdout: str) -> VerifyState:
    """Recover the freshness `state` from verify's stdout (the advisory exit code
    is always 0). An unrecognized line is recorded raw + flagged for manual
    review — never silently dropped. The wait token (if any) is recovered
    separately and attached — it is an annotation, not a state."""
    wait_seconds = parse_wait_seconds(stdout)
    for line in stdout.splitlines():
        for sub, state, intervention, flagged in _MARKERS:
            if sub in line:
                return VerifyState(state, intervention, flagged, line.strip(), wait_seconds)
    return VerifyState("raw", False, True, _last_nonblank(stdout), wait_seconds)


def _last_nonblank(s: str) -> str:
    for line in reversed(s.splitlines()):
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------------
# Running the real gate (STREAMING tee + byte-identical passthrough).
# --------------------------------------------------------------------------
def _stream_buffer(stream):
    """The byte sink behind sys.stdout/sys.stderr — the real `.buffer`, or the
    object itself for a test stand-in (_Cap) whose `.buffer` is a BytesIO."""
    return getattr(stream, "buffer", stream)


def run_real(raw_argv: list[str]) -> tuple[int, bytes, bytes]:
    """Run the real #30 wrapper with the unchanged argv, STREAMING both pipes
    line-by-line to our stdout/stderr as they arrive (#33 G4) while capturing the
    raw bytes for the row parse. Pre-#33 verify was instant, but the bounded
    verify-wait can now block for minutes on an in-flight produce — buffer-until-
    exit would make that look hung. Two pump threads (one per pipe) avoid the
    single-drain deadlock when output exceeds a pipe buffer. Byte-for-byte
    passthrough is preserved (each stream's bytes are emitted unchanged).

    On KeyboardInterrupt (the operator Ctrl-C's the push mid-wait) the child is
    terminated, the pumps drained, and the interrupt RE-RAISED so cmd_verify_wrap
    can still record a row (G4 — an aborted push is not a silent data drop)."""
    proc = subprocess.Popen([sys.executable, str(REAL_WRAPPER), *raw_argv],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_buf, err_buf = bytearray(), bytearray()

    def pump(src, buf, sink):
        try:
            for chunk in iter(src.readline, b""):
                buf += chunk
                sink.write(chunk)
                sink.flush()
        finally:
            try:
                src.close()
            except Exception:
                pass

    import threading
    t_out = threading.Thread(target=pump, args=(proc.stdout, out_buf, _stream_buffer(sys.stdout)))
    t_err = threading.Thread(target=pump, args=(proc.stderr, err_buf, _stream_buffer(sys.stderr)))
    t_out.start()
    t_err.start()
    try:
        proc.wait()
    except KeyboardInterrupt:
        # Ctrl-C during the wait: stop the child, drain what it emitted, re-raise.
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        raise
    t_out.join()
    t_err.join()
    return proc.returncode, bytes(out_buf), bytes(err_buf)


# --------------------------------------------------------------------------
# Measurement directory + canonical ledger.
# --------------------------------------------------------------------------
def measure_dir(root: Path) -> Path:
    override = os.environ.get("MERGE_GATE_MEASURE_DIR")
    return Path(override) if override else (root / ".scratch" / "merge-gate-measurement")


def ledger_path(root: Path) -> Path:
    return measure_dir(root) / "log.md"


# --------------------------------------------------------------------------
# Artefact location + snapshot (uses mg as a pure locator, never as an oracle).
# --------------------------------------------------------------------------
def locate_summary(root: Path, base_ref: str | None, base_sha: str | None,
                   tip_sha: str | None) -> tuple[str | None, str | None, dict | None]:
    """Recompute (base, diff_hash) exactly as cmd_verify does, then load the
    consumed summary.json. Best-effort: any failure degrades to (None, None, None)
    and the row is written without the machine snapshot."""
    if mg is None:
        return None, None, None
    try:
        cfg = mg.load_config(root)
        base = mg.resolve_base_sha(root, base_ref or cfg.base_ref, base_sha)
        if base is None:
            return None, None, None
        if tip_sha:
            cd = mg.canonical_diff_at_commit(root, base, tip_sha,
                                             cfg.review_globs, cfg.ignore_globs)
        else:
            cd = mg.canonical_diff(root, base, cfg.review_globs, cfg.ignore_globs)
        diff_hash = cd.get("diff_hash")
        if not diff_hash or cd.get("diff_error"):
            return base, diff_hash, None
        tdir = mg.tuple_dir(root / cfg.artifact_root, base, diff_hash)
        return base, diff_hash, mg.load_summary(tdir)
    except Exception:
        return None, None, None


# Citation snapshot/parse now has ONE owner — merge_gate_local (#40). These thin
# delegators keep this module's public surface (`read_citations` /
# `locate_citations`, consumed by build_row / cmd_verify_wrap and the measure
# tests) byte-stable, while the logic and the `[SEV] verdict id=<fid> file:line —
# citation` grammar live in the gate module (so they auto-mirror to keel and never
# duplicate). Both degrade to {} when the gate import is unavailable (mg is None).
def read_citations(tdir: Path) -> dict:
    return mg.read_citations(tdir) if mg is not None else {}


def locate_citations(root: Path, base: str | None, diff_hash: str | None) -> dict:
    return mg.locate_citations(root, base, diff_hash) if mg is not None else {}


def _is_canary(root: Path, tip_sha: str | None) -> bool:
    """A tip carrying a `Merge-Gate-Canary:` trailer is a calibration push,
    excluded from N (design §4)."""
    if mg is None or not tip_sha:
        return False
    try:
        return mg.tip_bypass_reason(root, tip_sha, "Merge-Gate-Canary") is not None
    except Exception:
        return False


# --------------------------------------------------------------------------
# ⓑ — post-commit producer state-dir reader (robust auto-produce volume).
# --------------------------------------------------------------------------
def read_scheduler_stats(root: Path) -> dict:
    """Read the post-commit producer's OWN durable state (never a shim):
    last_produce_ts and the count of completed background produces logged in
    produce.log (written by `merge_gate_post_commit.py launch_produce`). The
    `scheduler` in this name is legacy — the Stop scheduler it once read is
    retired (#33/ADR-0014); the source is now the post-commit producer. The name
    is kept for the `scheduler-stats` CLI contract + its tests."""
    state_root = Path(os.environ.get("MERGE_GATE_STATE_ROOT",
                                     str(Path.home() / ".claude" / "hooks" / ".merge-gate-state")))
    sdir = state_root / hashlib.sha1(str(root).encode()).hexdigest()[:16]
    last_produce_ts = 0.0
    try:
        st = json.loads((sdir / "state.json").read_text())
        last_produce_ts = float(st.get("last_produce_ts", 0) or 0)
    except Exception:
        pass
    produce_count = 0
    try:
        # Each completed background produce (the post-commit producer) appends
        # its summary line to produce.log.
        log = (sdir / "produce.log").read_text(errors="replace")
        produce_count = sum(1 for ln in log.splitlines() if "merge-gate produce: verdict=" in ln)
    except Exception:
        pass
    return {"last_produce_ts": last_produce_ts, "produce_count": produce_count}


def read_auto_produce_delta(root: Path) -> tuple[int, dict]:
    """ⓑ delta: completed auto-produces since the previous *committed* push row.
    Returns (delta, cursor) where cursor is the sidecar state to persist. The
    sidecar is NOT advanced here — the caller commits the cursor ONLY after the
    row is durably appended (see commit_auto_produce_cursor), so a failed ledger
    append never silently swallows the delta: it stays attributable to the next
    row instead of vanishing. Robust + deterministic."""
    stats = read_scheduler_stats(root)
    sidecar = measure_dir(root) / "measure-state.json"
    prev = 0
    try:
        prev = int(json.loads(sidecar.read_text()).get("produce_count", 0))
    except Exception:
        pass
    delta = stats["produce_count"] - prev
    cursor = {"produce_count": stats["produce_count"],
              "last_produce_ts": stats["last_produce_ts"]}
    return (delta if delta > 0 else 0), cursor


def commit_auto_produce_cursor(root: Path, cursor: dict) -> None:
    """Advance the ⓑ sidecar to the read cursor — called ONLY after the row is
    appended (F1), so a failed append leaves the delta for the next row."""
    sidecar = measure_dir(root) / "measure-state.json"
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(cursor))
    except Exception:
        pass


# --------------------------------------------------------------------------
# ⓒ — produce-intent side-file (best-effort manual force/intent capture).
# --------------------------------------------------------------------------
def record_produce_intent(root: Path, diff_hash: str | None, force: bool,
                          intent: str | None, ts: float) -> None:
    path = measure_dir(root) / "produce-intents.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"diff_hash": diff_hash, "force": bool(force),
                                "intent": intent or None, "ts": ts}) + "\n")
    except Exception:
        pass


def lookup_intent(root: Path, diff_hash: str | None) -> str | None:
    """ⓒ join: the most recent recorded intent/force for this diff_hash.
    force ⇒ manual by construction (the auto-producer never passes --force)."""
    if not diff_hash:
        return None
    path = measure_dir(root) / "produce-intents.jsonl"
    hit = None
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if rec.get("diff_hash") == diff_hash:
                hit = rec
    except Exception:
        return None
    if hit is None:
        return None
    if hit.get("intent"):
        return "intent"
    if hit.get("force"):
        return "force"
    return None


# --------------------------------------------------------------------------
# Row rendering (ledger schema — design §3).
# --------------------------------------------------------------------------
_BLOCK = {True: "✓", False: "✗"}


def _latency_line(summary: dict | None) -> str:
    if not summary or not summary.get("per_reviewer_timings"):
        return "- latency: —"
    parts = []
    total = 0.0
    for pr in summary["per_reviewer_timings"]:
        r = pr.get("reviewer_seconds") or 0
        v = pr.get("validator_seconds") or 0
        total += float(r) + float(v)
        parts.append(f"{pr.get('reviewer')} r={_secs(r)} v={_secs(v)}")
    return f"- latency: total={_secs(total)} · " + " · ".join(parts)


def _secs(x) -> str:
    try:
        f = float(x)
    except Exception:
        return "—"
    return f"{int(f)}s" if f == int(f) else f"{f:.1f}s"


def _findings_table(summary: dict | None) -> list[str]:
    header = [
        "| # | finding (file:line) | reviewers | sev | validator | block | conc | conf | rconf | human:real? | human:should-block? | same-as |",
        "|---|---------------------|-----------|-----|-----------|-------|------|------|-------|-------------|---------------------|---------|",
    ]
    if not summary:
        return header
    rows = []
    for i, f in enumerate(summary.get("findings", [])):
        loc = f"{f.get('file')}:{f.get('line_start')}"
        revs = "+".join(f.get("producing_reviewers") or [])
        rconf = f.get("reviewer_confidence")
        rconf_s = "—" if rconf is None else str(rconf)
        rows.append(
            f"| {i} | {loc} | {revs} | {f.get('severity')} | "
            f"{f.get('validator_verdict')} | {_BLOCK.get(bool(f.get('block')), '?')} | "
            f"{f.get('concordance_count')} | {f.get('confidence_score')} | {rconf_s} | "
            f"⬜ | ⬜ |  |"
        )
    return header + rows


def _citation_block(summary: dict | None, citations: dict | None) -> list[str]:
    """Render the validator-citation sub-block (design §3/§8) AFTER the findings
    table: one line per finding that HAS a snapshotted citation, keyed by finding
    index AND fid so it joins back unambiguously. The citation text is a durable
    snapshot of the transient validators.{json,md} (GC'd). Returns [] when there
    is nothing to render, so existing rows/tests are unaffected."""
    if not summary or not citations:
        return []
    findings = summary.get("findings", [])
    entries = []
    for i, f in enumerate(findings):
        fid = f.get("id")
        citation = citations.get(fid)
        if citation:
            verdict = f.get("validator_verdict", "—")
            entries.append(f"  - [{i}] {fid} · {verdict} · {citation}")
    if not entries:
        return []
    return ["",
            "- validator citations ⓐ (snapshot; source validators.{json,md} "
            "are GC-collected):"] + entries


def _advisory_conformance_line(summary: dict | None) -> str | None:
    """#35 (A: accept+measure) — surface per-reviewer soft-steer CONFORMANCE so a
    reviewer that produced NO usable signal (the claude advisory reviewer drifting
    to prose → `malformed-payload`, or `codex-failed`/`missing-result`/
    `normalize-failed`) is not silently read as 'the reviewer agreed / found
    nothing'. Read from `per_reviewer_timings[].codex_status`. Emitted ONLY when a
    reviewer's status is explicitly non-`ok` (a MISSING status — older/partial
    summaries — is treated as `ok`), so the common all-`ok` row stays byte-unchanged.

    The other soft-steer mode — the validator-side duplicate-id guard forcing a
    finding to fail-safe `unsure` (aggregate.py, distinct from the #29 Codex-side
    guard) — is already surfaced per-finding via its PRESERVED citation
    ('duplicate id in validator output (count=N)'), so it needs no separate line;
    advisory_signal_stats (merge_gate_adjudicate.py) counts both modes over
    organic N."""
    if not summary:
        return None
    degraded = [(pr.get("reviewer"), pr.get("codex_status"))
                for pr in summary.get("per_reviewer_timings", [])
                if (pr.get("codex_status") or "ok") != "ok"]
    if not degraded:
        return None
    parts = "; ".join(f"{r}={s}" for r, s in degraded)
    return f"- advisory conformance ⓓ: {parts} (no usable signal — soft-steer #35)"


def build_row(vs: VerifyState, summary: dict | None, base: str | None,
              diff_hash: str | None, tip_sha: str | None, reviewers: list[str],
              auto_since: int, intent: str | None, now: datetime,
              canary: bool, citations: dict | None = None) -> str:
    """Assemble one push section. Machine columns from summary.json; human:* and
    same-as left blank for finding-first, blind adjudication (design §3). When
    `citations` is non-empty a validator-citation sub-block is appended after the
    findings table (design §8 citation-preservation)."""
    pid = (tip_sha or diff_hash or "unknown")[:10]
    base10 = (base or "?")[:10]
    diff10 = (diff_hash or "?")[:10]
    when = "(canary)" if canary else now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if summary:
        revs = ", ".join(summary.get("reviewers") or reviewers)
        verdict = summary.get("verdict", "—")
        bc = summary.get("block_count", "—")
        codex_v = summary.get("codex_version") or "—"
        claude_m = summary.get("claude_model") or "—"
        schema = summary.get("schema_version", "—")
        enforcement = summary.get("enforcement_policy_at_produce", "advisory")
    else:
        revs = ", ".join(reviewers) if reviewers else "—"
        verdict = bc = codex_v = claude_m = schema = "—"
        enforcement = "advisory"

    interv = "yes" if vs.intervention else "no"
    flag = ""
    if vs.flagged:
        flag = f"   [flagged: {vs.raw_line}]" if vs.state == "raw" else f"   [flagged: {vs.state}]"
    # G4 — an aborted push (SIGINT during the in-flight-produce wait) is recorded
    # as a `missing` row flagged `wait-interrupted`, so it reads distinctly from a
    # clean miss (the operator adjudicates whether to re-push).
    if vs.raw_line == "wait-interrupted":
        flag = "   [flagged: wait-interrupted (push aborted during in-flight-produce wait)]"
    # #33 G1 — verify_wait_seconds: how long the push blocked on an in-flight
    # produce (the §5 latency/burden input). Omitted when 0 so pre-#33 rows and
    # the no-wait common case are byte-unchanged.
    wait = f"   wait={vs.wait_seconds}s" if getattr(vs, "wait_seconds", 0) else ""

    human_note = "—   (canary — not adjudicated)" if canary else "—"

    lines = [
        f"## push {pid} · {when} · {base10}..{diff10}",
        f"- reviewers: {revs}        verdict(gate): {verdict} · block_count={bc}",
        f"- freshness ⓐ: {vs.state}   intervention: {interv}{wait}{flag}",
        _latency_line(summary),
        f"- context: auto-produces-since-prev ⓑ={auto_since} · intent/force ⓒ={intent or '—'}",
        f"- machine: codex_version={codex_v} claude_model={claude_m} schema={schema} enforcement={enforcement}",
    ]
    adv = _advisory_conformance_line(summary)
    if adv:
        lines.append(adv)
    lines += [
        "- human push-verdict: " + human_note + "   ← captured finding-first",
        "",
    ]
    lines += _findings_table(summary)
    lines += _citation_block(summary, citations)
    return "\n".join(lines) + "\n"


def append_row(path: Path, section: str) -> None:
    """Append-only into the canonical ledger (the durable record of record)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + section)


ADJUDICATE_CMD = "python3 ~/.claude/scripts/merge_gate_adjudicate.py adjudicate"


def adjudication_reminder(summary: dict | None, canary: bool) -> str | None:
    """Push-time advisory: when a push CAPTURES findings, nudge the operator to
    run blind adjudication in a terminal at session end (design §3 rhythm).
    Adjudication itself is never automated — the human answers real?/should-block?
    blind, so this only surfaces that work is pending."""
    if canary:
        return None
    findings = (summary or {}).get("findings") or []
    if not findings:
        return None
    n = len(findings)
    return (
        f"merge-gate-measure: {n} finding(s) captured this push\n"
        f"  -> blind adjudication pending. At session end, in a terminal:\n"
        f"     {ADJUDICATE_CMD}"
    )


# --------------------------------------------------------------------------
# Subcommands.
# --------------------------------------------------------------------------
def _resolve_root(ns: argparse.Namespace) -> Path:
    """The repo root for ns: --cwd (or cwd), normalized to the git toplevel."""
    root = Path(ns.cwd or os.getcwd())
    if mg is not None:
        r = mg.repo_root(root)
        if r is not None:
            root = r
    return root


def _capture_row(ns: argparse.Namespace, vs: VerifyState) -> None:
    """Append one ledger row for this push from the recovered VerifyState (+ the
    summary snapshot). Shared by the normal path and the SIGINT path (G4). A
    `none` state (no in-scope changes) writes NO row — see the recursion note."""
    root = _resolve_root(ns)
    # No in-scope changes to gate → this push is not a measurement event, so
    # write no row. (a) Breaks the ledger-push recursion: because the measurement
    # dir is in the gate's `ignore_globs` (harness.toml), a ledger-only push has
    # an empty in-scope diff → state "none", so committing+pushing the ledger
    # spawns no further row and a clean tree is reachable. (b) Keeps push-N
    # counting only pushes the gate actually evaluated.
    if vs.state == "none":
        return
    base, diff_hash, summary = locate_summary(root, ns.base_ref, ns.base_sha, ns.tip_sha)
    reviewers = list(summary.get("reviewers") or []) if summary else _cfg_reviewers(root)
    auto_since, cursor = read_auto_produce_delta(root)
    intent = lookup_intent(root, diff_hash)
    canary = _is_canary(root, ns.tip_sha)
    citations = locate_citations(root, base, diff_hash)
    section = build_row(vs, summary, base, diff_hash, ns.tip_sha, reviewers,
                        auto_since, intent, datetime.now(timezone.utc), canary,
                        citations=citations)
    append_row(ledger_path(root), section)
    # Advance the ⓑ cursor ONLY now that the row is durably written (F1):
    # a failed append above skips this, leaving the delta for the next row.
    commit_auto_produce_cursor(root, cursor)
    # Nudge the operator (in-session, via stderr) to adjudicate when this push
    # captured findings. Advisory only — adjudication stays manual+blind.
    reminder = adjudication_reminder(summary, canary)
    if reminder:
        sys.stderr.write(reminder + "\n")


def cmd_verify_wrap(raw_argv: list[str], ns: argparse.Namespace) -> int:
    """ⓐ — the load-bearing push-time row writer. Behaviour-neutral: the real
    verify's exit code and bytes stream through unchanged; the capture is wrapped
    so nothing about measurement can ever break a push."""
    try:
        rc, out_bytes, err_bytes = run_real(raw_argv)
    except KeyboardInterrupt:
        # G4 — the operator Ctrl-C'd the push during the in-flight-produce wait.
        # Still record a row so the abort is not a silent measurement-data drop,
        # then exit 130 (SIGINT convention) so the push aborts cleanly.
        try:
            _capture_row(ns, VerifyState("missing", True, True, "wait-interrupted"))
        except Exception as e:
            sys.stderr.write(f"merge-gate-measure: interrupt row capture failed (non-blocking): {e}\n")
        return 130
    try:
        # run_real already streamed (teed) stdout/stderr live; just parse + record.
        vs = parse_verify_state(out_bytes.decode("utf-8", errors="replace"))
        _capture_row(ns, vs)
    except Exception as e:
        # Never break the push for a measurement failure (ADR-0009 behaviour-neutral).
        sys.stderr.write(f"merge-gate-measure: row capture failed (non-blocking): {e}\n")
    return rc


def cmd_produce_wrap(raw_argv: list[str], ns: argparse.Namespace) -> int:
    """ⓒ — manual produce shim. Records force/intent (force ⇒ manual) keyed by the
    diff_hash, then execs the real produce with byte-identical (streamed) passthrough."""
    import time
    try:
        root = _resolve_root(ns)
        diff_hash = _compute_diff_hash(root, ns.base_ref)
        force = bool(getattr(ns, "force", False))
        record_produce_intent(root, diff_hash, force, ns.intent, time.time())
    except Exception as e:
        sys.stderr.write(f"merge-gate-measure: intent capture failed (non-blocking): {e}\n")
    rc, _out, _err = run_real(raw_argv)
    return rc


def cmd_scheduler_stats(ns: argparse.Namespace) -> int:
    """ⓑ — inspect the post-commit producer's auto-produce state (read-only).
    (`scheduler-stats` subcommand name kept for back-compat — #33/ADR-0014.)"""
    root = _resolve_root(ns)
    print(json.dumps(read_scheduler_stats(root)))
    return 0


def _cfg_reviewers(root: Path) -> list[str]:
    if mg is None:
        return []
    try:
        return list(mg.load_config(root).reviewers)
    except Exception:
        return []


def _compute_diff_hash(root: Path, base_ref: str | None) -> str | None:
    """Mirror produce's diff_hash so ⓒ joins onto ⓐ's row. Best-effort."""
    if mg is None:
        return None
    try:
        cfg = mg.load_config(root)
        base = mg.resolve_base_sha(root, base_ref or cfg.base_ref)
        if base is None:
            return None
        cd = mg.canonical_diff(root, base, cfg.review_globs, cfg.ignore_globs)
        return cd.get("diff_hash")
    except Exception:
        return None


# --------------------------------------------------------------------------
# argparse — mirrors merge_gate_local.main so `pre-push.sh`'s argv parses
# identically (global --cwd, then the subcommand).
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="merge-gate-measure",
                                description="local-profile measurement capture wrapper (#31)")
    p.add_argument("--cwd", default=None, help="repo dir (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("verify", help="ⓐ wrap the real verify, capture the row")
    pv.add_argument("--base-sha", default=None)
    pv.add_argument("--tip-sha", default=None)
    pv.add_argument("--base-ref", default=None)
    pv.add_argument("--enforcement", default=None,
                    choices=["advisory", "client-side-blocking"])
    pv.set_defaults(kind="verify")

    pp = sub.add_parser("produce", help="ⓒ manual produce shim (records force/intent)")
    pp.add_argument("--base-ref", default=None)
    pp.add_argument("--tip-sha", default=None)     # forwarded verbatim (#33 4b)
    pp.add_argument("--coalesce", action="store_true")  # forwarded verbatim (#33)
    pp.add_argument("--intent", default=None)
    pp.add_argument("--intent-from", default=None)
    pp.add_argument("--force", action="store_true")
    pp.set_defaults(kind="produce")

    pf = sub.add_parser("force", help="alias for `produce --force`")
    pf.add_argument("--base-ref", default=None)
    pf.add_argument("--tip-sha", default=None)     # forwarded verbatim (#33 4b)
    pf.add_argument("--intent", default=None)
    pf.add_argument("--intent-from", default=None)
    pf.set_defaults(kind="produce", force=True)

    ps = sub.add_parser("scheduler-stats", help="ⓑ read the post-commit producer auto-produce state")
    ps.set_defaults(kind="scheduler-stats")
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    ns = build_parser().parse_args(raw)
    if ns.kind == "verify":
        return cmd_verify_wrap(raw, ns)
    if ns.kind == "produce":
        return cmd_produce_wrap(raw, ns)
    if ns.kind == "scheduler-stats":
        return cmd_scheduler_stats(ns)
    return 0  # pragma: no cover - argparse `required` precludes this


if __name__ == "__main__":
    sys.exit(main())
