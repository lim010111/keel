#!/usr/bin/env python3
"""auto_fill — scaffold-doctor #04, the thin-delegator fill dispatcher.

The "fill" half of /harness-doctor. It COMPOSES the existing setup skills' own
apply paths (never re-implements a write), inherits their consent ladder as
deterministic per-action tiers, hard-refuses the footguns the survey found
(ADR-0020 §5), and separates repo scope (default, auto-applied) from a one-time
global-bootstrap behind a single consent.

Two inputs (ADR-0020 §4): the read-only engine's diagnose() records (per-concern
fillability oracle) AND read_recorded_profile() (scaffold membership). The
fillable set is {diagnose gaps} ∩ {recorded scaffold}; with no [harness] recorded
auto_fill is a no-op that points at /harness-doctor.

Staged API so no Python callable crosses the script↔skill seam:
  build_plan(repo)              -> a JSON-serializable plan (pure; no writes)
  apply(repo)                   -> applies the scope=repo auto tier itself
  apply_confirmed(repo, ids)    -> the skill calls this after its go-ahead
  global_bootstrap(...)         -> the one-time global writes, behind consent
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# Resolve harness_doctor (the read-only engine) from ~/.claude/scripts so the CLI
# works as a standalone subprocess — the way the skill invokes it — not only under
# a test's pre-set sys.path. <this file> is skills/harness-doctor/scripts/auto_fill.py,
# so parents[3] is ~/.claude and parents[3]/scripts holds harness_doctor.py.
_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import harness_doctor

# merge-gate hook + marker facts, read from the engine's enforcement registry —
# the doctor-side owner (a contract test pins it equal to install_local's
# writer-side constants). auto_fill never imports marker strings from
# install_local directly.
_PRE_PUSH_HOOK, _PRE_PUSH_MARKER = harness_doctor.GATE_ENFORCEMENT["merge-gate"]
_POST_COMMIT_HOOK, _POST_COMMIT_MARKER = harness_doctor.MEASUREMENT_HOOK


# --------------------------------------------------------------------------
# Consent tiers — deterministic from each setup skill's OWN action KIND alone
# (the consent ladder transplanted from setup-agents-md SKILL.md §3, made
# content-testable so it is not model discretion). The kind vocabulary IS the
# installer-action contract (documented at setup_agents_md.plan): pure-create/
# additive (change) -> auto; content-moving (migrate) -> confirm; conflict
# (warn) -> refuse; already-in-place (ok) -> dropped. The message is human-
# facing text, never sniffed — the old `msg.startswith("migrate ")` substring
# contract is retired (a sibling reword can no longer downgrade consent).
# --------------------------------------------------------------------------
def _tier(kind):
    if kind == "change":
        return "auto"
    if kind == "migrate":
        return "confirm"
    return "refuse"          # warn / error / unknown kind -> safe default (report-only)


def _report_only(concern, message, scope="repo"):
    """A surfaced-but-not-auto-filled record (refuse tier, carries no apply)."""
    return {"concern": concern, "scope": scope, "consent_tier": "refuse",
            "kind": "report", "message": message, "action_id": f"{concern}:report"}


def _action_records(concern, scope, actions):
    """Map a setup skill's (kind, msg[, apply_fn]) action tuples to per-action
    plan records. 'ok' actions (already in place) are dropped — nothing to fill."""
    records = []
    for i, entry in enumerate(actions):
        kind, msg = entry[0], entry[1]
        if kind == "ok":
            continue
        if kind == "error":
            # A broken delegate (e.g. agents-md templates missing) — surface
            # report-only, agreeing with the engine's intent='n/a' (not a gap), in
            # the report/refuse vocabulary the skill documents.
            records.append(_report_only(concern, f"{msg} (skill install incomplete)", scope))
            continue
        records.append({
            "concern": concern,
            "scope": scope,
            "consent_tier": _tier(kind),
            "kind": kind,
            "message": msg,
            "action_id": f"{concern}:{i}",
        })
    return records


def _agents_md_actions(repo):
    """Re-run setup_agents_md.plan() read-only to obtain its (kind, msg, apply_fn)
    action tuples — diagnose() carries no kind/message, so the fill path re-plans
    (delegation, not re-detection). The target is repo-contained (auto_fill's
    responsibility — plan() bypasses main()'s relative_to guard)."""
    mod = harness_doctor.import_setup("setup-agents-md", "setup_agents_md")
    target = harness_doctor.find_repo_root(repo) or Path(repo)
    actions = []
    mod.plan(target, actions)
    return mod, actions


# --------------------------------------------------------------------------
# merge-gate — bespoke branch. It has no tuple-emitting planner, so its tier is
# derived from the engine's enforcement axis + a read-only foreign-hook preflight
# (not from action tuples). Delegated to install_local's FOUR repo-scoped
# functions directly — never install_local.main() (which also runs the GLOBAL
# deregister_stale_hooks). Templates resolved from the skill (main()'s defaults).
# --------------------------------------------------------------------------
_MG_SKILL = "setup-merge-gate"


def _merge_gate_mod():
    return harness_doctor.import_setup(_MG_SKILL, "install_local")


def _mg_templates():
    tdir = harness_doctor.CLAUDE_ROOT / "skills" / _MG_SKILL / "templates"
    return tdir / "pre-push.sh", tdir / "post-commit"


def _git_common_dir(repo):
    """The git COMMON dir (shared `.git` across linked worktrees), resolved, or
    None. A linked worktree's hooks live here, NOT under the worktree working path."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return (Path(repo) / out).resolve()      # `out` is relative to repo, or absolute


def _hooks_dir_in_repo(repo):
    """True iff git's resolved hooks dir belongs to THIS repo — inside the worktree
    OR inside the git common dir (a linked worktree's hooks resolve to the shared
    .git/hooks, outside the worktree path but still the same repo). An absolute
    core.hooksPath escaping BOTH is a two-scope leak -> merge-gate report-only."""
    repo = Path(repo).resolve()
    hd = harness_doctor.resolve_hooks_dir(repo).resolve()
    for base in (repo, _git_common_dir(repo)):
        if base is None:
            continue
        try:
            hd.relative_to(base)
            return True
        except ValueError:
            pass
    return False


def _merge_gate_decision(repo, rec):
    """Read-only fill decision for the merge-gate concern. status is one of:
      'wired'        — pre-push already carries our marker -> in place, dropped
      'report-only'  — parked / legacy / unrecognized profile / out-of-repo hooks
                       dir -> surfaced, never auto-filled (footgun / ADR-0009)
      'fill'         — auto-fillable; tier auto (clean) or confirm (a foreign hook
                       will be backed up). install_pre_push/install_post_commit
                       flags gate EACH hook re-render on its own marker (symmetric
                       footgun guard — never an in-place clobber of a marked hook)."""
    if rec["enforcement"] == "wired":
        # enforcement keys on the pre-push marker ALONE. Drop (fully in place) only
        # when the #33 post-commit producer trigger is ALSO wired; otherwise SURFACE
        # the partial state (pre-push enforcing, producer missing) report-only — #04
        # never re-renders the wired pre-push nor surgically installs the missing
        # post-commit (that stays #07).
        if harness_doctor.hook_has_marker(repo, _POST_COMMIT_HOOK, _POST_COMMIT_MARKER):
            return {"status": "wired"}    # both hooks marked -> already in place
        return {"status": "report-only",
                "message": "merge-gate pre-push wired but the #33 post-commit "
                           "producer trigger is missing — report-only; the surgical "
                           "post-commit-only fill is #07"}
    # Fillable predicate. Excludes state-bearing (unknown-class) and
    # unrecognized profiles (intent=partial — anything but 'local', the only
    # profile since ADR-0021). What remains is profile local-or-absent — the
    # engine has already classified every other case out.
    if not (rec["applicability"] == "applicable" and rec["state"] is None
            and rec["intent"] != "partial"):
        return {"status": "report-only",
                "message": "merge-gate not auto-fillable (unrecognized "
                           "[merge-gate] — expected profile 'local'); resolve "
                           "via /setup-merge-gate"}
    if not _hooks_dir_in_repo(repo):
        return {"status": "report-only",
                "message": "git hooks dir resolves outside the repo "
                           "(core.hooksPath) — report-only (two-scope leak)"}
    pp_present = harness_doctor.hook_has_marker(repo, _PRE_PUSH_HOOK, _PRE_PUSH_MARKER)
    pc_present = harness_doctor.hook_has_marker(repo, _POST_COMMIT_HOOK, _POST_COMMIT_MARKER)
    hd = harness_doctor.resolve_hooks_dir(repo)
    foreign = ((hd / _PRE_PUSH_HOOK).exists() and not pp_present) or \
              ((hd / _POST_COMMIT_HOOK).exists() and not pc_present)
    return {
        "status": "fill",
        "tier": "confirm" if foreign else "auto",
        "message": "install local merge-gate (harness.toml local profile + "
                   "pre-push + post-commit + .gitignore)",
        "install_pre_push": not pp_present,
        "install_post_commit": not pc_present,
    }


def _merge_gate_records(repo, rec):
    d = _merge_gate_decision(repo, rec)
    if d["status"] == "wired":
        return []                          # in place -> nothing to fill (like 'ok')
    if d["status"] == "report-only":
        return [{"concern": "merge-gate", "scope": "repo", "consent_tier": "refuse",
                 "kind": "report", "message": d["message"], "action_id": "merge-gate:0"}]
    return [{"concern": "merge-gate", "scope": "repo", "consent_tier": d["tier"],
             "kind": "change", "message": d["message"], "action_id": "merge-gate:0"}]


def _apply_merge_gate(repo, rec, tiers):
    d = _merge_gate_decision(repo, rec)
    if d["status"] != "fill" or d["tier"] not in tiers:
        return False
    mg = _merge_gate_mod()
    pp_tpl, pc_tpl = _mg_templates()
    mg.write_harness_toml(repo)                    # local profile; preserves [harness]
    if d["install_pre_push"]:
        mg.install_pre_push(repo, pp_tpl)
    if d["install_post_commit"]:
        mg.install_post_commit(repo, pc_tpl)
    mg.ensure_gitignore(repo)
    return True


# --------------------------------------------------------------------------
# status-harness — delegated to install_project(root, actions, apply). Its writes
# happen INLINE when apply=True (no per-action apply_fn to filter), and it leaves
# any `warn` (diverged vendored file) untouched — so install_project(apply=True)
# IS the correct partial delegation: change sub-files applied (auto), warn
# preserved (refuse). Its action tuples are (kind, msg) 2-tuples.
# --------------------------------------------------------------------------
def _status_vendor_source_ok(mod):
    """True iff a status.py vendor SOURCE is readable — the global copy OR the
    skill's bundled snapshot. install_project reads the source at its TOP even
    with apply=False, so a missing source would crash the read-only plan; this
    guard makes status-harness report-only instead (run the global-bootstrap)."""
    return mod.STATUS_PY.exists() or mod.BUNDLED.exists()


def _status_harness_records(repo):
    mod = harness_doctor.import_setup("setup-status-harness", "setup_status_harness")
    if not _status_vendor_source_ok(mod):
        return [_report_only("status-harness", "no readable status.py vendor source "
                             "(global or bundled) — run the global-bootstrap first")]
    root = harness_doctor.find_repo_root(repo) or Path(repo)
    actions = []
    mod.install_project(root, actions, False)
    return _action_records("status-harness", "repo", actions)


def _apply_status_harness(repo, tiers):
    if "auto" not in tiers:
        return False
    mod = harness_doctor.import_setup("setup-status-harness", "setup_status_harness")
    if not _status_vendor_source_ok(mod):
        return False                         # report-only — never crash on a missing source
    root = harness_doctor.find_repo_root(repo) or Path(repo)
    detect = []
    mod.install_project(root, detect, False)
    if not any(a[0] == "change" for a in detect):
        return False
    # install_project(apply=True) writes the change sub-files and leaves any warn
    # (diverged) file untouched — the correct partial delegation.
    mod.install_project(root, [], True)
    return True


def build_plan(repo):
    """Return the read-only fill plan for `repo` (writes nothing). With no
    [harness] profile recorded, this is a no-op that points at /harness-doctor —
    it never falls back to filling raw diagnose() gaps."""
    repo = harness_doctor.find_repo_root(repo) or Path(repo)   # accept a subdir; resolve to git root
    profile = harness_doctor.read_recorded_profile(repo)
    if profile is None:
        return {
            "repo": str(repo),
            "profile_recorded": False,
            "records": [],
            "notes": ["No [harness] profile recorded — run /harness-doctor to "
                      "record one first (no fill without a recorded scaffold)."],
        }
    diag = {r["concern"]: r for r in harness_doctor.diagnose(repo)}
    records = []
    for concern in dict.fromkeys(profile["scaffold"]):  # dedup a hand-edited repeated slug
        if concern == "agents-md":
            _, actions = _agents_md_actions(repo)
            records += _action_records("agents-md", "repo", actions)
        elif concern == "merge-gate" and concern in diag:
            records += _merge_gate_records(repo, diag[concern])
        elif concern == "status-harness":
            records += _status_harness_records(repo)
        elif concern == "self-verification":
            records.append(_report_only(
                "self-verification", "dormant per ADR-0009 and has no installer to "
                "delegate to (only hooks/templates/commit-msg) — report-only, never "
                "auto-activated"))
        else:
            # A recorded slug with no auto-fill path (the merge-gate-local slug trap,
            # a hand-edit, a future concern): surfaced report-only, NEVER silently
            # dropped and never filled.
            records.append(_report_only(
                concern, "recorded scaffold slug with no auto-fill path (no installer "
                "/ no detector) — report-only"))
    records += _global_records(profile["scaffold"])   # scope=global, one bootstrap consent
    return {"repo": str(repo), "profile_recorded": True, "records": records, "notes": []}


def _stale_hooks_present(mg, settings_path):
    """Read-only mirror of deregister_stale_hooks's question — are the RETIRED Stop
    scheduler / PostToolUse mark registrations present? Reuses install_local's own
    SCHED_CMD / MARK_CMD constants (no re-implemented command strings) so the
    global-bootstrap can 'detect pending first' for a writer that has no apply=False
    mode of its own."""
    p = Path(settings_path)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event, retired in (("Stop", mg.SCHED_CMD), ("PostToolUse", mg.MARK_CMD)):
        for g in hooks.get(event, []) or []:
            for h in g.get("hooks", []):
                if h.get("command") == retired:
                    return True
    return False


def _global_pending():
    """True iff operator-global setup is pending — the status-harness global layer
    is not fully installed, OR retired stale Stop/PostToolUse registrations remain.
    Read-only. A blocked detect (invalid settings.json) counts as pending (it needs
    attention)."""
    sh = harness_doctor.import_setup("setup-status-harness", "setup_status_harness")
    mg = harness_doctor.import_setup("setup-merge-gate", "install_local")
    detect = []
    if not sh.install_global(detect, False):
        return True
    return any(k == "change" for k, _ in detect) or _stale_hooks_present(mg, sh.SETTINGS)


# Concerns with a global (~/.claude) dimension — the global layer is only relevant
# to a repo whose recorded scaffold includes one of these (status-harness needs the
# global status.py + SessionStart/Stop hooks; merge-gate's stale-hook cleanup is
# global hygiene). An agents-md-only repo never surfaces global setup.
_GLOBAL_DIMENSION_CONCERNS = {"status-harness", "merge-gate"}


def _global_records(scaffold):
    """A single scope='global' record when global setup pends AND the scaffold has a
    global-dimension concern — batched under the skill's ONE global-bootstrap consent
    (scope='global' overrides the per-action tier; never part of the per-repo
    auto-apply)."""
    if not (set(scaffold) & _GLOBAL_DIMENSION_CONCERNS):
        return []
    if not _global_pending():
        return []
    return [{
        "concern": "global-bootstrap", "scope": "global", "consent_tier": "confirm",
        "kind": "change",
        "message": "operator-global setup pending (status.py + SessionStart/Stop "
                   "hooks and/or stale-hook cleanup in ~/.claude) — run the one-time "
                   "global-bootstrap (single consent)",
        "action_id": "global-bootstrap:0",
    }]


def global_bootstrap():
    """Perform the operator-global ~/.claude writes — status-harness install_global
    (status.py + SessionStart/Stop hooks) and merge-gate deregister_stale_hooks —
    consolidated into ONE step the skill gates behind a SINGLE explicit consent.

    Both halves target the SAME settings file (status-harness's module-level
    SETTINGS — install_global takes no path arg), so the stale-hook probe + cleanup
    and the hook install never disagree on which settings.json they touch.

    Detects pending work read-only first and is a content-checked no-op when the
    global layer is already in place. Returns a structured summary; on a failed
    install_global (e.g. an invalid ~/.claude/settings.json) it returns
    status='blocked' (a structured signal, never a silent half-write or a crash)."""
    sh = harness_doctor.import_setup("setup-status-harness", "setup_status_harness")
    mg = harness_doctor.import_setup("setup-merge-gate", "install_local")
    settings_path = sh.SETTINGS

    detect = []
    if not sh.install_global(detect, False):       # read-only detect (no writes)
        return {"status": "blocked",
                "reason": "status-harness global detect failed — ~/.claude/"
                          "settings.json is not valid JSON or the bundled "
                          "status.py snapshot is missing"}
    sh_pending = any(k == "change" for k, _ in detect)
    stale_pending = _stale_hooks_present(mg, settings_path)
    if not sh_pending and not stale_pending:
        return {"status": "noop", "status_harness": "in-place", "stale_hooks": "none"}

    if not sh.install_global([], True):            # apply the global layer
        return {"status": "blocked",
                "reason": "install_global write failed (~/.claude/settings.json "
                          "invalid or unwritable)"}
    removed = mg.deregister_stale_hooks(Path(settings_path))
    return {"status": "applied",
            "status_harness": "installed" if sh_pending else "in-place",
            "stale_hooks_removed": removed}


def apply(repo, tiers=("auto",), only_concerns=None):
    """Apply the repo-scope actions whose consent_tier is in `tiers` (default the
    auto tier — auto_fill applies these itself; confirm/global go through the
    skill). `only_concerns`, when given, restricts the apply to those concern slugs
    (used by apply_confirmed). Each concern is filled by handing its OWN apply loop
    a FILTERED action list — reuse, never a re-implemented write. Global writes are
    NEVER done here (only global_bootstrap). Returns a small summary."""
    repo = harness_doctor.find_repo_root(repo) or Path(repo)   # accept a subdir; resolve to git root
    profile = harness_doctor.read_recorded_profile(repo)
    if profile is None:
        return {"applied": []}
    diag = {r["concern"]: r for r in harness_doctor.diagnose(repo)}
    applied = []
    for concern in dict.fromkeys(profile["scaffold"]):  # dedup a hand-edited repeated slug
        if only_concerns is not None and concern not in only_concerns:
            continue
        if concern == "agents-md":
            mod, actions = _agents_md_actions(repo)
            filtered = [a for a in actions
                        if a[0] in ("change", "migrate") and _tier(a[0]) in tiers]
            if filtered:
                mod.apply_actions(filtered)
                applied.append(concern)
        elif concern == "merge-gate" and concern in diag:
            if _apply_merge_gate(repo, diag[concern], tiers):
                applied.append(concern)
        elif concern == "status-harness":
            if _apply_status_harness(repo, tiers):
                applied.append(concern)
    return {"applied": applied}


def apply_confirmed(repo, action_ids):
    """Apply the CONFIRM-tier work for the concerns named by `action_ids` — called
    by SKILL.md AFTER its AskUserQuestion go-ahead. Re-derives from repo state (no
    callable was persisted from build_plan); the action_id's concern prefix selects
    what to apply."""
    concerns = {aid.split(":", 1)[0] for aid in action_ids}
    return apply(repo, tiers=("confirm",), only_concerns=concerns)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="auto_fill",
        description="scaffold-doctor #04 — auto-fill dispatcher. Default: emit the "
                    "JSON fill plan (read-only). The /harness-doctor skill drives "
                    "the consent prompts; this CLI is the wire.")
    ap.add_argument("path", nargs="?", default=None,
                    help="target repo (defaults to cwd's git root)")
    ap.add_argument("--json", action="store_true",
                    help="emit the JSON fill plan (read-only; the default)")
    ap.add_argument("--apply", action="store_true",
                    help="apply the repo-scope AUTO tier (auto_fill's own writes)")
    ap.add_argument("--apply-confirmed", nargs="+", metavar="ACTION_ID", default=None,
                    help="apply the CONFIRM-tier work for these action_ids (the skill "
                         "calls this after its go-ahead)")
    ap.add_argument("--global-bootstrap", action="store_true",
                    help="run the one-time operator-global bootstrap (writes ~/.claude)")
    args = ap.parse_args(argv)

    if args.global_bootstrap:
        print(json.dumps(global_bootstrap(), indent=2))
        return 0

    root = harness_doctor.find_repo_root(args.path)
    if root is None:
        print("ERROR: not inside a git repository "
              "(git rev-parse --show-toplevel failed).", file=sys.stderr)
        return 1

    if args.apply:
        print(json.dumps(apply(root), indent=2))
    elif args.apply_confirmed:
        print(json.dumps(apply_confirmed(root, args.apply_confirmed), indent=2))
    else:                       # default + --json: the read-only plan
        print(json.dumps(build_plan(root), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
