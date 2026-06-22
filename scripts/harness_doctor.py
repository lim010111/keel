#!/usr/bin/env python3
"""harness_doctor — Tier-1 read-only scaffold-conformance engine (scaffold-doctor #02).

Introspects a target repo's harness scaffold and reports it on two axes (intent vs
enforcement) with **no writes**. The reusable core CI, the /harness-doctor skill
(Tier-2), and phase-2 integrity all import directly. Design: ADR-0020.
"""
import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

# The engine's public surface. Everything here is a supported seam for the
# Tier-2/3 consumers (auto_fill, record_profile, the /harness-doctor skill,
# CI, phase-2 integrity); `_`-prefixed names are engine-internal and may
# change without notice.
__all__ = [
    "CLAUDE_ROOT", "GATE_ENFORCEMENT", "MEASUREMENT_HOOK",
    "SCAFFOLD_CONCERNS",
    "find_repo_root", "diagnose", "propose_profile", "read_recorded_profile",
    "compute_coverage", "render_table", "render_coverage",
    "import_setup", "resolve_hooks_dir", "hook_has_marker",
    "hook_enforcement_state", "main",
]

CLAUDE_ROOT = Path(__file__).resolve().parent.parent


def import_setup(skill, module):
    """Import a setup skill's module by file path (no sys.path pollution), so the
    doctor REUSES its detection rather than re-implementing it (ADR-0020 §4)."""
    path = CLAUDE_ROOT / "skills" / skill / "scripts" / f"{module}.py"
    spec = importlib.util.spec_from_file_location(module, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _intent_from_kinds(kinds):
    """Map a setup skill's action-kind set to an intent. `ok`=already in place,
    `change`/`migrate`=work to do, `warn`=present-but-differs. On the INTENT axis
    a migrate is just pending work — the change/migrate split matters to consent
    (auto_fill's tiers), not to presence — so it folds into `change` here. A pure
    mix → partial; only-pending-work → absent; only-ok → present. (Single-file
    states 2/3 of agents-md surface as `absent` — plan() emits no `ok` for the
    existing file — an accepted imprecision of reuse over re-detection.)"""
    kinds = {"change" if k == "migrate" else k for k in kinds} - {"error"}
    if not kinds or kinds == {"ok"}:
        return "present"
    if kinds == {"change"}:
        return "absent"
    return "partial"


def _reused_intent_record(concern, skill, module, run, warn_is_drift=False):
    """Build an intent-axis record by REUSING a setup skill's own detector. `run`
    takes the imported module and returns its action list (apply=False). On import
    OR detection failure, or an `error` action, the concern is n/a, never a false
    present/absent (a raising detector must not crash the read-only doctor).

    `warn_is_drift`: for the status-harness detector, a `warn` action normally
    means "vendored file present but differs from its ~/.claude source" = DRIFT,
    not a half-install. When every non-`ok` action is a `warn`, the concern is
    reported `state="drifted"` (intent present — the files ARE installed) and rides
    the engine's `state is not None → not a gap` rule: report-only, never a
    false-flagged gap (ADR-0030 §1,3). The discriminator (§3): the moment a
    `change` (a genuinely MISSING file) appears it stays a real intent gap. For
    agents-md, `warn` is a CONFLICT (both files, no @import) — a real gap, NOT
    drift — so the default `warn_is_drift=False` keeps it collapsing to partial.

    KNOWN EDGE (routed to #07, near-unreachable): install_project emits ONE
    non-drift `warn` — "doc template missing … skill installation is incomplete" —
    when the skill's OWN bundled template is absent (a corrupted ~/.claude, the
    tree the doctor itself runs from). That warn reads here as `drifted` rather
    than a broken install. The honest fix needs a distinct writer-side action
    subtype (message-sniffing is a retired anti-pattern); tracked in #07, not
    patched in this read-only engine."""
    try:
        mod = import_setup(skill, module)
        actions = run(mod)
    except Exception:
        return _record(concern, intent="n/a", applicability="n/a",
                       detail=f"{skill} detector unavailable")
    kinds = {a[0] for a in actions}
    if "error" in kinds:
        return _record(concern, intent="n/a", detail=f"{skill} reported an error")
    non_ok = kinds - {"ok"}
    if warn_is_drift and non_ok and non_ok <= {"warn"}:
        return _record(concern, intent="present", enforcement="n/a", state="drifted",
                       detail="vendored file(s) differ from ~/.claude source "
                              "(report-only; ADR-0030)")
    return _record(concern, intent=_intent_from_kinds(kinds), enforcement="n/a")


def _agents_md_record(repo_root):
    """agents-md intent via setup_agents_md.plan() — pure in-tree concern (no hook,
    enforcement n/a). plan() collects actions but writes nothing."""
    def run(mod):
        actions = []
        mod.plan(repo_root, actions)
        return actions
    return _reused_intent_record("agents-md", "setup-agents-md", "setup_agents_md", run)


def _status_harness_record(repo_root):
    """status-harness intent via setup_status_harness.install_project(apply=False).
    NEVER calls install_global (which would mutate ~/.claude) — only the project
    layer, with apply=False so nothing is written."""
    def run(mod):
        actions = []
        mod.install_project(repo_root, actions, False)
        return actions
    return _reused_intent_record("status-harness", "setup-status-harness",
                                 "setup_status_harness", run, warn_is_drift=True)


# Declarative-by-class: a gate section is resolved to its enforcement hook +
# marker through this registry. The markers are not derivable from the section
# name, so one lookup is irreducible (ADR-0020 §4) — adding a gate costs +1 line
# here, not a change to diagnose()'s iteration. A declared section absent from
# this map is still reported (enforcement = n/a, unknown-class).
# This registry is the DOCTOR-side owner of the marker fact (auto_fill reads
# markers from here, never from install_local directly); the writer-side owner
# is install_local's PRE_PUSH_MARKER/POST_COMMIT_MARKER, and a contract test in
# test_harness_doctor pins the two equal.
GATE_ENFORCEMENT = {
    "merge-gate": ("pre-push", "MERGE_GATE_WRAPPER"),
}

# The #33 measurement producer trigger (post-commit). NOT a gate — it is wiring
# that feeds #31 evidence — so it is folded into merge-gate's detail, never its
# own concern. The merge-gate enforcement verdict keys on pre-push, not this.
MEASUREMENT_HOOK = ("post-commit", "MERGE_GATE_POST_COMMIT")

# Top-level harness.toml sections that are NOT gates, so the generic gate
# iteration must skip them (no enforcement marker, no unknown-class report).
NON_GATE_SECTIONS = {"harness"}  # ADR-0020 §3 intended-profile meta (#03)

# The canonical per-concern probe IDs (#03 AC11) — EXACTLY the `concern` keys
# diagnose() emits, so a recorded [harness] `scaffold` list intersects cleanly
# with the diagnose records (the merge-gate ≠ merge-gate-local trap). `ci` is
# deliberately absent: it is a judgment row with no mechanical detector, recorded
# as a [harness] bool and reported "wanted / not-yet-measurable", never a term in
# the coverage fraction. A `scaffold` slug outside this set is `unrecognized`.
SCAFFOLD_CONCERNS = ("agents-md", "status-harness", "merge-gate")


def resolve_hooks_dir(repo_root):
    """Git's real hooks dir (honors core.hooksPath, linked worktrees, GIT_DIR),
    not an assumed <repo>/.git/hooks. Ported from install_local.py's
    `_resolve_hooks_dir`. Falls back to <repo>/.git/hooks only when git can't answer."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return repo_root / ".git" / "hooks"
    return repo_root / out


def hook_has_marker(repo_root, hook_name, marker):
    """True iff the resolved <hook_name> hook exists and carries `marker`."""
    hook = resolve_hooks_dir(repo_root) / hook_name
    if not hook.exists():
        return False
    try:
        return marker in hook.read_text(encoding="utf-8")
    except Exception:
        return False


def hook_enforcement_state(repo_root, hook_name, marker):
    """Enforcement verdict for <hook_name> on the marker (phase-2 integrity,
    ADR-0030 §5). One of:
      'wired'    — marker present AND user-executable (git will run it)
      'broken'   — marker present BUT not user-executable; git won't run it (B1)
      'unwired'  — hook absent, unreadable, or marker-less (a fresh clone / foreign)

    The executable-bit check is POSIX-guarded: where the bit is meaningless (a
    Windows checkout, os.name != 'posix') a marked hook is 'wired' on the marker
    alone — the honest verdict there. Hook-BODY / template-version staleness is
    deliberately OUT of scope (ADR-0030 §5): operator customization is expected,
    so a body-diff can't be honestly attested as stale; integrity stops here."""
    if not hook_has_marker(repo_root, hook_name, marker):
        return "unwired"
    hook = resolve_hooks_dir(repo_root) / hook_name
    try:
        mode = hook.stat().st_mode
    except OSError:
        # A stat race after the marker read (file removed / permission flip): the
        # exec-check is best-effort, so degrade to the marker-confirmed verdict
        # rather than crash the read-only doctor (#02 B2: diagnose never raises).
        return "wired"
    if os.name == "posix" and not (mode & stat.S_IXUSR):
        return "broken"
    return "wired"


class _UnparseableConfig(dict):
    """Sentinel for a present-but-unreadable harness.toml (#07 AC4). A dict
    SUBCLASS, not None: it stays an EMPTY dict, so the two _load_toml call sites
    that `.get()` / iterate keep working (`.get(...)` -> None, iteration yields
    nothing) while diagnose()/read_recorded_profile() can `isinstance`-branch on it.
    `reason` carries the ACTUAL cause — a genuine TOML syntax error vs an I/O /
    permission / version-skew read failure — so the report names the real fault
    instead of always blaming TOML syntax (merge-gate review of bc0fa11,
    claude:finding-0). Without this sentinel, _load_toml returned a plain `{}` for
    BOTH absent and unreadable, so a broken config false-reported as intent=absent —
    a false-clean conflicting with ADR-0020's honesty decisions."""

    def __init__(self, reason=""):
        super().__init__()
        self.reason = reason


def _load_toml(repo_root):
    """Parsed <repo>/harness.toml. Returns {} when ABSENT; an empty
    _UnparseableConfig sentinel (carrying the cause) when PRESENT-BUT-UNREADABLE
    (#07 AC4) — distinct so a broken/unreadable config is never read as a clean
    'absent'. The catch is intentionally broad (diagnose must NEVER raise — the #02
    B2 read-only contract), but the cause is DISTINGUISHED: a real TOMLDecodeError
    (syntax) vs any other read failure (permission / I/O / version-skew), so the
    report names the real fault rather than always blaming TOML syntax. Never raises."""
    path = repo_root / "harness.toml"
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        return _UnparseableConfig(f"invalid TOML syntax: {e}")
    except Exception as e:
        # NOT a syntax error (permission / I/O / version-skew). Still a sentinel
        # (never an absent-config false-clean), but the report must not blame TOML
        # syntax for a file that could not even be read (claude:finding-0).
        return _UnparseableConfig(f"could not read harness.toml: {e}")


def _record(concern, intent="absent", enforcement="n/a",
            applicability="applicable", state=None, detail=""):
    return {
        "concern": concern,
        "intent": intent,
        "enforcement": enforcement,
        "applicability": applicability,
        "state": state,
        "detail": detail,
    }


def _merge_gate_record(repo_root, mg):
    """merge-gate is bespoke: 'local' is the only profile (ADR-0021); anything
    else is surfaced, never blessed. Always reported (the marquee gate) — absent
    when there is no [merge-gate] section."""
    if mg is None:
        return _record("merge-gate", intent="absent", enforcement="n/a")
    profile = mg.get("profile")
    if profile == "local":
        hook, marker = GATE_ENFORCEMENT["merge-gate"]
        enforcement = hook_enforcement_state(repo_root, hook, marker)
        # The #33 measurement post-commit is a producer trigger (wiring), not the
        # enforcement gate — its detail keys on marker-presence only (no exec-bit).
        m_hook, m_marker = MEASUREMENT_HOOK
        m_state = "wired" if hook_has_marker(repo_root, m_hook, m_marker) else "unwired"
        return _record("merge-gate", intent="present", enforcement=enforcement,
                       detail=f"#33 measurement post-commit trigger: {m_state}")
    # Unrecognized: a typo'd profile, an empty [merge-gate], a profile-less
    # section, or a leftover github-actions/legacy-GHA config (the profile was
    # removed — ADR-0021). Surface as an applicable gap (intent partial, state
    # None so _is_gap fires) rather than false-blessing it — and never as a
    # local gate with a missing hook (which would let #04 auto-install onto it).
    return _record(
        "merge-gate", intent="partial", enforcement="n/a",
        detail=f"unrecognized [merge-gate] profile {profile!r}; expected 'local'")


def _gate_record(repo_root, section):
    """A declared gate section resolved generically through GATE_ENFORCEMENT:
    probe its hook-marker (a registered gate is reported intent=present)."""
    hook, marker = GATE_ENFORCEMENT[section]
    enforcement = hook_enforcement_state(repo_root, hook, marker)
    return _record(section, intent="present", enforcement=enforcement)


def diagnose(repo_root):
    """Read-only two-axis scaffold report for `repo_root` — a list of records."""
    data = _load_toml(repo_root)
    records = []

    # Base doc/structure scaffold (always reported), detected by reuse-by-import.
    # These are FILESYSTEM-detected (AGENTS.md / vendored files), so an unparseable
    # harness.toml does not affect them — they stay above the sentinel branch.
    records.append(_agents_md_record(repo_root))
    records.append(_status_harness_record(repo_root))

    # Present-but-unreadable harness.toml (#07 AC4): we cannot read ANY
    # config-derived concern (merge-gate + any declared gate sections). Surface it
    # as a distinct, actionable state — NEVER let the absent-config path read the
    # missing [merge-gate] as a clean intent=absent (a false-clean). _is_gap treats
    # config-unparseable as a gap, so the doctor exits non-zero rather than "conforms".
    # The detail names the REAL cause (data.reason) and states that ALL config-derived
    # concerns are unreadable, so the single row is not misread as "only merge-gate
    # is affected" (merge-gate review of bc0fa11, claude:finding-0 + finding-1).
    if isinstance(data, _UnparseableConfig):
        records.append(_record(
            "merge-gate", intent="n/a", enforcement="n/a", state="config-unparseable",
            detail=f"harness.toml present but unreadable ({data.reason}) — no "
                   f"config-derived concern (merge-gate + any declared gate sections) "
                   f"could be read"))
        return records

    # merge-gate: bespoke schema discrimination, always reported.
    records.append(_merge_gate_record(repo_root, data.get("merge-gate")))

    # Every other declared top-level section, iterated generically: a registered
    # gate is probed; an unregistered one still surfaces as unknown-class (never
    # silently dropped); non-gate meta sections are skipped (ADR-0020 §4).
    for section in data:
        if section == "merge-gate" or section in NON_GATE_SECTIONS:
            continue
        if section in GATE_ENFORCEMENT:
            records.append(_gate_record(repo_root, section))
        else:
            records.append(_record(
                section, intent="present", enforcement="n/a", state="unknown-class",
                detail="declared gate section with no registered detector (+1 "
                       "GATE_ENFORCEMENT line to cover it)"))

    return records


def _has_git_remote(repo_root):
    """True iff `repo_root` has at least one configured git remote (read-only)."""
    try:
        out = subprocess.run(["git", "-C", str(repo_root), "remote"],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return bool(out.strip())


# Build manifests that signal "buildable code" → surfaces the CI judgment row.
# A representative set (not exhaustive); a repo can want CI without a manifest,
# which is why CI is a confirmed judgment, not a mechanical scaffold member.
BUILD_MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")


def _has_build_manifest(repo_root):
    """True iff a recognized build manifest sits at `repo_root` (read-only)."""
    return any((Path(repo_root) / m).is_file() for m in BUILD_MANIFESTS)


def propose_profile(repo_root):
    """Read-only candidate intended-scaffold for `repo_root` from mechanical
    signals (#03 AC1) — returns a proposal dict, writes nothing. The operator
    confirms the judgment rows (the skill's interview) before any record."""
    scaffold = ["agents-md", "status-harness"]   # base doc/structure (AC2)
    if _has_git_remote(repo_root):               # remote → merge-gate candidate (AC3)
        scaffold.append("merge-gate")
    return {
        "scaffold": scaffold,
        "ci": _has_build_manifest(repo_root),    # build manifest → CI row (AC4)
    }


def read_recorded_profile(repo_root):
    """The recorded intended-scaffold `[harness]` section, or None when absent
    (AC13/AC14). Read-only — sits on the existing _load_toml (no second file
    open, never a write). DEGRADES malformed external data instead of raising
    (the _load_toml / compute_coverage house idiom): a non-table `harness` is
    treated as no profile; a `scaffold` that is not a list of strings yields an
    empty list (never list("agents-md") → char-exploded slugs); a non-bool `ci`
    is dropped to None. A `[harness]` with no `scaffold` key defaults to []."""
    h = _load_toml(repo_root).get("harness")
    if not isinstance(h, dict):
        return None        # non-table `harness = "x"` is not a recorded profile
    raw = h.get("scaffold")
    scaffold = [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
    ci = h.get("ci")
    return {"scaffold": scaffold, "ci": ci if isinstance(ci, bool) else None}


def compute_coverage(records, profile):
    """Read-only coverage of a recorded `profile` against `diagnose()` records
    (#03 AC12). Pure — no I/O. Coverage = |installed ∩ scaffold| ÷ |scaffold|;
    installed = intent==present (intent axis)."""
    # Coverage is set-valued — dedupe the recorded scaffold (order-preserving) so
    # a hand-edited [harness] with a repeated slug does not double-count.
    scaffold = list(dict.fromkeys(profile.get("scaffold", [])))
    rec_by = {r["concern"]: r for r in records if "concern" in r}
    # The recognized scaffold concerns are the denominator. A recorded slug
    # outside SCAFFOLD_CONCERNS (the merge-gate-local trap, a hand-edit, or a
    # future slug) has no diagnose record — it is surfaced as `unrecognized`,
    # NEVER folded into the denominator where it would be an uncloseable false gap.
    recognized = [c for c in scaffold if c in SCAFFOLD_CONCERNS]
    unrecognized = [c for c in scaffold if c not in SCAFFOLD_CONCERNS]
    covered = [c for c in recognized if rec_by.get(c, {}).get("intent") == "present"]
    return {
        "covered": sorted(covered),
        "applicable": sorted(recognized),
        "fraction": (len(covered), len(recognized)),
        "measurable": len(recognized) > 0,   # False on an empty/malformed scaffold (no denominator)
        "unrecognized": sorted(unrecognized),
        # Recorded judgment rows with no mechanical detector — reported "wanted /
        # not-yet-measurable", echoed from the record, never re-derived from a
        # live signal and never a term in the fraction above.
        "judgments": {"ci": profile.get("ci")},
    }


def _is_gap(rec, ci=False):
    """A gap = an *applicable* concern that is not fully in place. An `n/a`
    concern (a reused detector that was unavailable) is never a gap;
    unknown-class / drifted (state is not None) is informational, not a gap.

    `ci=True` (the --ci intent-only mode, ADR-0030 §4) excludes the enforcement
    axis: a fresh clone with intent present but the git hook unwired/broken is a
    legitimate CI state (ADR-0020 Decision 2 — enforcement is machine-local,
    never CI-asserted), so only the INTENT axis can make it a gap there. The
    non-ci default is unchanged: locally an unwired/broken hook stays a fillable
    gap (auto-fill wants to wire it)."""
    # A present-but-unparseable harness.toml (#07 AC4) is a real, actionable gap on
    # BOTH axes — checked BEFORE the `state is not None -> not a gap` rule that
    # unknown-class / drifted ride, so a broken config is never a false-clean. It is
    # an intent-axis defect (the portable in-tree config is broken), so `ci` never
    # relaxes it either.
    if rec["state"] == "config-unparseable":
        return True
    if rec["applicability"] != "applicable" or rec["state"] is not None:
        return False
    if rec["intent"] in ("absent", "partial"):
        return True
    if ci:
        return False
    return rec["enforcement"] in ("unwired", "broken")


def render_table(records):
    """Human-readable two-axis table."""
    cols = f"{'concern':<18} {'intent':<8} {'enforce':<9} {'applic.':<11} state"
    lines = [cols]
    for r in records:
        lines.append(f"{r['concern']:<18} {r['intent']:<8} {r['enforcement']:<9} "
                     f"{r['applicability']:<11} {r['state'] or ''}".rstrip())
    return "\n".join(lines)


def render_coverage(coverage):
    """Human-readable coverage block for a recorded [harness] profile (#03)."""
    n, d = coverage["fraction"]
    if coverage["measurable"]:
        lines = [f"coverage: {n}/{d} applicable concern(s) installed "
                 f"({round(100 * n / d)}%)"]
        missing = sorted(set(coverage["applicable"]) - set(coverage["covered"]))
        if missing:
            lines.append(f"  missing: {', '.join(missing)}")
    else:
        lines = ["coverage: no measurable scaffold concerns recorded"]
    if coverage["unrecognized"]:
        lines.append(f"  unrecognized in scaffold (no probe): "
                     f"{', '.join(coverage['unrecognized'])}")
    ci = coverage["judgments"].get("ci")
    if ci is not None:
        lines.append(f"  ci: wanted={'yes' if ci else 'no'} (not-yet-measurable)")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="harness_doctor",
        description="Read-only scaffold-conformance report for a target repo "
                    "(scaffold-doctor #02). Writes nothing.")
    ap.add_argument("path", nargs="?", default=None,
                    help="repo to inspect (defaults to cwd's git root)")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable JSON report")
    ap.add_argument("--ci", action="store_true",
                    help="intent-axis-only mode (ADR-0030 §4): exclude the "
                         "enforcement axis (machine-local git hooks) from the gap "
                         "count and exit code, so a fresh clone with config present "
                         "but hooks unwired exits 0. Enforcement is still REPORTED, "
                         "labelled out-of-scope. The local default gates on it.")
    ap.add_argument("--non-interactive", action="store_true",
                    help="explicit headless signal (the skill passes this in CI): "
                         "suppress the interactive first-run proposal hint. The "
                         "engine is non-prompting regardless — only the skill "
                         "decides whether to run the AskUserQuestion interview "
                         "(headless is signalled here, NOT inferred from --json).")
    args = ap.parse_args(argv)

    root = find_repo_root(args.path)
    if root is None:
        print("ERROR: not inside a git repository "
              "(git rev-parse --show-toplevel failed).", file=sys.stderr)
        return 1

    records = diagnose(root)
    gaps = [r for r in records if _is_gap(r, ci=args.ci)]
    exit_code = 2 if gaps else 0   # exit stays keyed on #02 gaps; coverage is informational (AC: #03 adds the fraction without changing #02's exit semantics). --ci drops the enforcement axis from the gap set (ADR-0030 §4)

    # A recorded [harness] profile turns on the coverage lens (AC13); with no
    # profile there is no denominator — raw per-concern presence only (AC14).
    # main() NEVER calls propose_profile — proposal is interactive-first-run-only,
    # the skill's job (AC15/AC17/AC18).
    profile = read_recorded_profile(root)
    coverage = compute_coverage(records, profile) if profile else None

    if args.json:
        payload = {"repo": str(root), "concerns": records, "gaps": len(gaps),
                   "exit": exit_code}
        if args.ci:
            payload["intent_only"] = True   # --ci: enforcement reported but not gated
        if coverage is not None:
            payload["coverage"] = coverage
        print(json.dumps(payload, indent=2))
    else:
        print(f"harness-doctor — {root}")
        print(render_table(records))
        print()
        if args.ci:
            print("intent-only (--ci): the enforcement axis (machine-local git "
                  "hooks) is REPORTED above but out of scope for the gap count "
                  "and exit code (ADR-0030 §4).")
        print(f"{len(gaps)} gap(s) on applicable concerns."
              if gaps else "Scaffold conforms — no gaps on applicable concerns.")
        if coverage is not None:
            print()
            print(render_coverage(coverage))
        elif not args.non_interactive:
            # First run, no recorded profile: point at the interview. Suppressed
            # under the explicit headless signal (AC14/AC17/AC18). The engine
            # never proposes here — proposal is the skill's interactive job.
            print()
            print("No [harness] profile recorded — run /harness-doctor "
                  "interactively to propose and record one (no coverage "
                  "denominator until then).")
    return exit_code


def find_repo_root(start=None):
    """Git toplevel for `start` (or cwd when None), or None if not in a repo.

    Mirrors the shared helper in setup_agents_md.py / setup_status_harness.py; adds
    an optional `start` dir so the engine (and its tests) can probe an arbitrary
    target repo rather than only cwd."""
    cmd = ["git", "rev-parse", "--show-toplevel"]
    if start is not None:
        cmd = ["git", "-C", str(start), "rev-parse", "--show-toplevel"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


if __name__ == "__main__":
    sys.exit(main())
