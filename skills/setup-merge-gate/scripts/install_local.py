#!/usr/bin/env python3
"""install_local.py — deterministic local-profile install for /setup-merge-gate
(claude-harness-work #30 D7/D8). The SKILL orchestrates (HITL, profile
detection); this helper does the file writes:

  * merge the `[merge-gate]` + `[merge-gate.local*]` sections into harness.toml
    (D8 defaults), leaving any `[merge-gate.github-actions]` block and unrelated
    tables/comments byte-untouched;
  * install the per-repo pre-push hook;
  * add the artefact cache to .gitignore;
  * register the global Stop / PostToolUse hooks in settings.json (idempotent);
  * (authorized freeze exception) tear down an installed GHA workflow when
    switching a repo to local — the SKILL HITL-confirms before passing
    --teardown-gha.

Run: install_local.py --repo <path> [--settings <path>] [--pre-push-template <path>]
                      [--teardown-gha]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Resolve toml_sections (the shared section-scoped TOML text utilities) from
# ~/.claude/scripts so this script works standalone: <this file> is
# skills/setup-merge-gate/scripts/install_local.py, so parents[3] is ~/.claude.
_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from toml_sections import section_is, section_name, split_sections  # noqa: E402

# Canonical local sections (D8). Kept as text so comments survive and the
# defaults are auditable. The section editor below owns these table names.
LOCAL_TABLES = (
    "[merge-gate.local]",
    "[merge-gate.local.scheduler]",
    "[merge-gate.local.producer]",
    "[merge-gate.local.producer.codex]",
)

LOCAL_BLOCK = """\
[merge-gate.local]
enforcement_policy  = "advisory"        # advisory | client-side-blocking
freshness_policy    = "content"         # content | tool-strict
base_ref            = "auto"
artifact_root       = ".codex-review/local"
review_globs        = ["**/*"]
ignore_globs        = [".codex-review/**"]   # minimal; lockfiles stay IN scope
blocking_severities = ["critical", "high"]
bypass_trailer      = "Merge-Gate-Bypass"

[merge-gate.local.scheduler]
auto_produce         = "stop-debounced"      # off | stop-debounced
debounce_seconds     = 90
min_interval_seconds = 600

[merge-gate.local.producer]
reviewers = ["codex"]                   # ordered reviewer set (ADR-0010)

[merge-gate.local.producer.codex]
bin = "codex"
"""


# --------------------------------------------------------------------------
# harness.toml — section-aware merge (stdlib only; no TOML writer dependency).
# split_sections/section_is live in the shared scripts/toml_sections.py (one
# owner for the splitting + header-equivalence logic; record_profile.py is the
# other consumer). Header comparison folds TOML-equivalent spellings, so an
# existing `[ merge-gate ]` is edited in place, never duplicated.
# --------------------------------------------------------------------------
def merge_harness_toml(existing: str) -> str:
    """Return harness.toml text with the local profile selected. Idempotent.
    `[merge-gate.github-actions]` and every unrelated table/comment are
    preserved verbatim (D8 — installer writes only the local sections).

    Existing `[merge-gate.local*]` tables are PRESERVED verbatim, never
    clobbered (C2): a repo that promoted enforcement_policy to
    "client-side-blocking" or customized reviewers/base_ref must not be
    silently reverted on reinstall. Canonical defaults (LOCAL_BLOCK) are
    appended only on a fresh install (no pre-existing local tables); runtime
    load_config fills any missing keys from DEFAULT_CONFIG."""
    blocks = split_sections(existing)
    local_names = {section_name(t) for t in LOCAL_TABLES}
    has_local = any(section_name(h) in local_names for h, _ in blocks if h)
    out: list[str] = []
    have_merge_gate = False
    for header, lines in blocks:
        if section_is(header, "merge-gate"):
            have_merge_gate = True
            # Force profile = "local"; keep any other keys/comments in the block.
            new_lines = []
            wrote_profile = False
            for ln in lines:
                if ln.strip().startswith("profile") and "=" in ln:
                    new_lines.append('profile = "local"\n')
                    wrote_profile = True
                else:
                    new_lines.append(ln)
            if not wrote_profile:
                # insert profile right after the header line
                new_lines = [new_lines[0], 'profile = "local"\n'] + new_lines[1:]
            out.append("".join(new_lines))
        else:
            # LOCAL_TABLES fall through here and are preserved verbatim.
            out.append("".join(lines))
    text = "".join(out)
    if not have_merge_gate:
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
        text += '[merge-gate]\nprofile = "local"\n'
    if not text.endswith("\n"):
        text += "\n"
    if not has_local:
        text += "\n" + LOCAL_BLOCK
    return text


def write_harness_toml(repo_root: Path) -> Path:
    path = repo_root / "harness.toml"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(merge_harness_toml(existing), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# pre-push hook
# --------------------------------------------------------------------------
# Stable marker the merge-gate pre-push template carries; used to tell our own
# hook apart from a foreign one (husky / pre-commit / secret-scan / lint).
PRE_PUSH_MARKER = "MERGE_GATE_WRAPPER"

# Set by install_pre_push when it backs up a pre-existing foreign hook, so
# main() can surface it. None when nothing was backed up.
_last_pre_push_backup: Path | None = None


def _resolve_hooks_dir(repo_root: Path) -> Path:
    """Resolve git's REAL hooks directory instead of assuming <repo>/.git/hooks
    (F1 + the C3 residual). `git rev-parse --git-path hooks` honors core.hooksPath
    (Husky and friends set it), linked worktrees and submodules (where .git is a
    FILE — the old <repo>/.git/hooks both crashed on mkdir AND, worse, left the
    hook in a dir git never reads while silently skipping the C3 foreign-hook
    backup), and GIT_DIR. For a plain single-worktree repo this returns the
    identical <repo>/.git/hooks, so the common case is unchanged.

    Falls back to <repo>/.git/hooks only when git can't answer (not a repo) — a
    state main() already rejects before this runs, so the fallback is defensive,
    never the gate-disabling path."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return repo_root / ".git" / "hooks"
    # --git-path returns a path relative to repo_root (we ran with -C) or an
    # absolute path (absolute core.hooksPath); `repo_root / out` is correct for
    # both — pathlib discards the left operand when `out` is already absolute.
    return repo_root / out


# Stable marker the merge-gate post-commit template carries (#33), used the same
# way as PRE_PUSH_MARKER to tell our own hook apart from a foreign one.
POST_COMMIT_MARKER = "MERGE_GATE_POST_COMMIT"

# Set by install_post_commit when it backs up a pre-existing foreign hook.
_last_post_commit_backup: Path | None = None


def _install_hook(repo_root: Path, hook_name: str, marker: str, template: Path):
    """Install <template> into the repo's resolved <hook_name> hook, backing up a
    pre-existing FOREIGN hook (one without `marker`) to <hook_name>.pre-merge-gate
    — never clobbering a prior backup. A hook that already carries `marker` is
    ours → overwritten in place. Returns (dest, backup_or_None)."""
    hooks_dir = _resolve_hooks_dir(repo_root)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / hook_name
    backup_made = None
    if dest.exists():
        existing = dest.read_text(encoding="utf-8")
        if marker not in existing:
            backup = dest.with_name(f"{hook_name}.pre-merge-gate")
            if not backup.exists():
                backup.write_text(existing, encoding="utf-8")
                # Preserve the foreign hook's mode bits (typically 0o755) so a
                # restore-by-rename yields an executable hook git will run (C3
                # safety-semantics residual; write_text would leave it 0o644).
                os.chmod(backup, os.stat(dest).st_mode & 0o777)
                backup_made = backup
    dest.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(dest, 0o755)
    return dest, backup_made


def install_pre_push(repo_root: Path, template: Path) -> Path:
    global _last_pre_push_backup
    dest, _last_pre_push_backup = _install_hook(
        repo_root, "pre-push", PRE_PUSH_MARKER, template)
    return dest


def install_post_commit(repo_root: Path, template: Path) -> Path:
    """Install the #33 produce trigger (post-commit) — same marker + foreign-backup
    convention as install_pre_push. post-commit / pre-push / pre-commit are
    distinct git events, so the per-hook installer never collides across gates
    (ADR-0014)."""
    global _last_post_commit_backup
    dest, _last_post_commit_backup = _install_hook(
        repo_root, "post-commit", POST_COMMIT_MARKER, template)
    return dest


# --------------------------------------------------------------------------
# .gitignore
# --------------------------------------------------------------------------
def ensure_gitignore(repo_root: Path, entry: str = ".codex-review/") -> bool:
    gi = repo_root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if any(ln.strip() == entry for ln in lines):
        return False
    with open(gi, "a", encoding="utf-8") as fh:
        if lines and lines[-1].strip():
            fh.write("\n")
        fh.write(f"# merge-gate-local artefact cache (not committed — #30 D3)\n{entry}\n")
    return True


# --------------------------------------------------------------------------
# global hook registration (idempotent)
# --------------------------------------------------------------------------
MARK_CMD = "python3 " + str(Path.home() / ".claude" / "hooks" / "merge_gate_mark.py")
SCHED_CMD = "python3 " + str(Path.home() / ".claude" / "hooks" / "merge_gate_scheduler.py")


def _has_command(groups: list, command: str) -> bool:
    for g in groups:
        for h in g.get("hooks", []):
            if h.get("command") == command:
                return True
    return False


def deregister_stale_hooks(settings_path: Path) -> bool:
    """Remove the RETIRED Stop scheduler + PostToolUse mark registrations from
    settings.json (#33/ADR-0014 replaced them with the per-repo post-commit hook).
    Idempotent; preserves every unrelated hook; prunes hook groups it empties and
    any event list left empty. Returns True if anything changed."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except Exception:
        data = {}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, retired in (("Stop", SCHED_CMD), ("PostToolUse", MARK_CMD)):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for g in groups:
            kept = [h for h in g.get("hooks", []) if h.get("command") != retired]
            if len(kept) != len(g.get("hooks", [])):
                changed = True
            if kept:
                new_groups.append({**g, "hooks": kept})
            # a group emptied by the removal is dropped (changed already flagged)
        if new_groups:
            hooks[event] = new_groups
        elif event in hooks:
            del hooks[event]
            changed = True
    if changed:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return changed


# --------------------------------------------------------------------------
# Uninstall — remove the local gate's per-repo hooks + stale global registrations.
# --------------------------------------------------------------------------
def _uninstall_hook(repo_root: Path, hook_name: str, marker: str) -> dict:
    """Remove our <hook_name> hook (one carrying `marker`); if a
    <hook_name>.pre-merge-gate foreign backup exists, restore it in its place.
    A foreign hook WITHOUT our marker is left untouched. Returns a small summary."""
    hooks_dir = _resolve_hooks_dir(repo_root)
    dest = hooks_dir / hook_name
    backup = hooks_dir / f"{hook_name}.pre-merge-gate"
    result = {"removed": False, "restored": False}
    if dest.exists() and marker in dest.read_text(encoding="utf-8"):
        if backup.exists():
            mode = os.stat(backup).st_mode & 0o777
            os.replace(backup, dest)      # restore the foreign hook
            os.chmod(dest, mode)
            result["restored"] = True
        else:
            dest.unlink()
            result["removed"] = True
    return result


def uninstall(repo_root: Path, settings_path: Path) -> dict:
    """Reverse the local-profile hook install: remove our pre-push + post-commit
    hooks (restoring a foreign backup when present) and deregister the stale
    global Stop/PostToolUse registrations. harness.toml + .gitignore are left for
    the operator (the SKILL documents removing them by hand)."""
    return {
        "pre_push": _uninstall_hook(repo_root, "pre-push", PRE_PUSH_MARKER),
        "post_commit": _uninstall_hook(repo_root, "post-commit", POST_COMMIT_MARKER),
        "stale_global_hooks_removed": deregister_stale_hooks(settings_path),
    }


# --------------------------------------------------------------------------
# GHA teardown — the single operator-authorized freeze exception (ADR-0009).
# --------------------------------------------------------------------------
def teardown_gha(repo_root: Path) -> list[str]:
    """Remove an installed GHA merge-gate workflow when switching to local.
    Returns the list of removed paths (for the install output). The SKILL
    HITL-confirms before calling this."""
    removed = []
    wf = repo_root / ".github" / "workflows" / "codex-review.yml"
    if wf.exists():
        wf.unlink()
        removed.append(str(wf.relative_to(repo_root)))
    return removed


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="local-profile installer helper (#30)")
    p.add_argument("--repo", required=True)
    p.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    p.add_argument("--pre-push-template",
                   default=str(Path(__file__).resolve().parent.parent / "templates" / "pre-push.sh"))
    p.add_argument("--post-commit-template",
                   default=str(Path(__file__).resolve().parent.parent / "templates" / "post-commit"))
    p.add_argument("--teardown-gha", action="store_true")
    p.add_argument("--uninstall", action="store_true",
                   help="remove the local gate's pre-push + post-commit hooks and "
                        "deregister the stale Stop/PostToolUse global hooks (#33)")
    args = p.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        sys.stderr.write(f"install_local: {repo} is not a git repo\n")
        return 1

    if args.uninstall:
        result = uninstall(repo, Path(args.settings))
        print(json.dumps({"repo": str(repo), "uninstall": result}, indent=2))
        print("\nNOTE: harness.toml's [merge-gate.local*] sections and the "
              ".gitignore entry are left in place — remove them by hand if you "
              "want the repo fully clean.")
        return 0

    write_harness_toml(repo)
    install_pre_push(repo, Path(args.pre_push_template))
    pre_push_backup = _last_pre_push_backup
    install_post_commit(repo, Path(args.post_commit_template))
    post_commit_backup = _last_post_commit_backup
    ensure_gitignore(repo)
    # The Stop scheduler + PostToolUse mark are RETIRED (#33/ADR-0014); the
    # per-repo post-commit hook replaces them. Clean any stale registrations.
    stale_removed = deregister_stale_hooks(Path(args.settings))
    removed = teardown_gha(repo) if args.teardown_gha else []

    print(json.dumps({
        "repo": str(repo),
        "harness_toml": "written (local profile)",
        "pre_push": "installed",
        "pre_push_backup": (str(pre_push_backup.relative_to(repo))
                            if pre_push_backup else None),
        "post_commit": "installed (auto-produce trigger — #33)",
        "post_commit_backup": (str(post_commit_backup.relative_to(repo))
                               if post_commit_backup else None),
        "gitignore": "ensured",
        "stale_global_hooks_removed": stale_removed,
        "gha_workflow_removed": removed,
    }, indent=2))
    for backup, name in ((pre_push_backup, "pre-push"),
                         (post_commit_backup, "post-commit")):
        if backup:
            print(f"\nNOTE: an existing {name} hook was backed up to "
                  f"{backup.relative_to(repo)} before installing the merge-gate hook.")
    if args.teardown_gha:
        print("\nNOTE: GHA workflow teardown is the single operator-authorized "
              "freeze exception (ADR-0009 § freeze Exception, 2026-05-28).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
