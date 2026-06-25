#!/usr/bin/env python3
"""Bootstrap the AGENTS.md ↔ CLAUDE.md relationship in a repo or subdir.

AGENTS.md is the canonical source — Codex CLI and antigravity CLI both read
it directly, the same way Claude reads CLAUDE.md. CLAUDE.md `@import`s
AGENTS.md so the two stay in sync without duplicating content.

This script only sets up the *relationship*; authoring the content of
AGENTS.md is the project's job.

States handled at each directory:
  1. Neither file        → create both (AGENTS.md from template, CLAUDE.md @import wrapper)
  2. Only CLAUDE.md      → move CLAUDE.md content into AGENTS.md, replace
                           CLAUDE.md with @import wrapper (git holds the
                           original, so no .bak file)
  3. Only AGENTS.md      → create CLAUDE.md @import wrapper
  4. Both files          → if CLAUDE.md already imports AGENTS.md, silent
                           no-op. Else refuse: print ⚠ and let the human
                           decide how to merge.

Recursive by default: with no PATH it sweeps the whole repo — the sweep root
(always planned, so a fresh repo still bootstraps root guidance) plus every
git-tracked-or-untracked-not-ignored directory that already holds a CLAUDE.md
or AGENTS.md. A positional PATH scopes the sweep to that subtree; `--single`
operates on exactly one directory (the pre-recursive behavior). `plan()` is
the single-directory planner and is unchanged — harness-doctor calls it
directly.

After wiring, the *AGENTS.md graph* is made internally coherent: path-shaped
CLAUDE.md cross-references inside AGENTS.md files (link target, link text,
inline-code path) whose target directory is now wired flip to AGENTS.md, so
Codex / antigravity follow one consistent set of files. Bare-word prose is
left alone. References that live OUTSIDE the graph (source comments, READMEs,
.claude/agents, CI templates) are *reported, never edited* — the human owns
that blast radius.

Idempotent: every state's wired-up path reports ✓ and writes nothing on
rerun, and the cross-link pass finds nothing left to flip. Output uses the
same ✓ / + / ⚠ markers as setup-status-harness.

Usage:
  setup_agents_md.py [--dry-run] [--single] [PATH]
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
AGENTS_TEMPLATE = TEMPLATES / "AGENTS.md.template"
CLAUDE_TEMPLATE = TEMPLATES / "CLAUDE.md.template"

# Matches an @import line for AGENTS.md, with or without the `./` prefix.
# Anchored to its own line so we don't false-match `@AGENTS.md` mid-prose.
IMPORT_RE = re.compile(r"^@\.?/?AGENTS\.md\s*$", re.MULTILINE)


def find_repo_root():
    """Return the git toplevel path, or None if cwd is not in a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def has_agents_import(claude_text):
    return bool(IMPORT_RE.search(claude_text))


def retitle_claude_h1(text):
    """When migrating CLAUDE.md content into AGENTS.md, rewrite a leading
    boilerplate `# CLAUDE.md` title line to `# AGENTS.md` so the new file is not
    titled after the old one (scaffold-doctor#10). Only the document's leading H1
    is touched, and only when it is the literal filename-title — a meaningful
    custom title and every body mention of CLAUDE.md are preserved byte-for-byte."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "":
            continue              # skip leading blank lines to the title
        if line.strip() == "# CLAUDE.md":
            lines[i] = "# AGENTS.md"
        break                      # inspect only the first non-blank line
    return "\n".join(lines)


def plan(target, actions):
    """Append (kind, msg, apply_fn) tuples describing what to do at `target`.

    The kind vocabulary is a DOWNSTREAM CONTRACT (harness-doctor's auto_fill
    derives consent tiers from it; the doctor's engine maps it to an intent):
      ok      — already in place, nothing to do
      change  — pure-create / additive write (safe to auto-apply)
      migrate — a content-MOVING change (State-2: CLAUDE.md → AGENTS.md);
                semantically distinct from `change` so consumers can require
                an explicit go-ahead without sniffing the message text
      warn    — conflict; refuse to auto-edit, human merges
      error   — the skill install itself is broken (templates missing)"""
    agents = target / "AGENTS.md"
    claude = target / "CLAUDE.md"

    agents_exists = agents.exists()
    claude_exists = claude.exists()

    if not AGENTS_TEMPLATE.exists() or not CLAUDE_TEMPLATE.exists():
        actions.append(("error", f"template files missing under {TEMPLATES}", None))
        return

    agents_tpl = AGENTS_TEMPLATE.read_text(encoding="utf-8")
    claude_tpl = CLAUDE_TEMPLATE.read_text(encoding="utf-8")

    if not agents_exists and not claude_exists:
        # State 1: neither exists.
        def apply_state1():
            agents.write_text(agents_tpl, encoding="utf-8")
            claude.write_text(claude_tpl, encoding="utf-8")
        actions.append(("change", f"create {_rel(agents)} (template)", apply_state1))
        actions.append(("change", f"create {_rel(claude)} (@import wrapper)", None))
        return

    if claude_exists and not agents_exists:
        # State 2: migrate CLAUDE.md → AGENTS.md, replace CLAUDE.md with wrapper.
        claude_text = claude.read_text(encoding="utf-8")
        if has_agents_import(claude_text):
            # Odd but possible: CLAUDE.md already imports AGENTS.md but AGENTS.md
            # is missing. Create the AGENTS.md template and leave CLAUDE.md alone.
            def apply_state2_dangling():
                agents.write_text(agents_tpl, encoding="utf-8")
            actions.append((
                "change",
                f"create {_rel(agents)} — CLAUDE.md already imports it but the file is missing",
                apply_state2_dangling,
            ))
            return

        def apply_state2():
            agents.write_text(retitle_claude_h1(claude_text), encoding="utf-8")
            claude.write_text(claude_tpl, encoding="utf-8")
        actions.append((
            "migrate",
            f"migrate {_rel(claude)} → {_rel(agents)} (content moved, CLAUDE.md becomes @import wrapper)",
            apply_state2,
        ))
        return

    if agents_exists and not claude_exists:
        # State 3: AGENTS.md only — create the wrapper.
        def apply_state3():
            claude.write_text(claude_tpl, encoding="utf-8")
        actions.append(("change", f"create {_rel(claude)} (@import wrapper)", apply_state3))
        return

    # State 4: both exist.
    claude_text = claude.read_text(encoding="utf-8")
    if has_agents_import(claude_text):
        actions.append(("ok", f"{_rel(claude)} already imports AGENTS.md"))
        actions.append(("ok", f"{_rel(agents)} present"))
        return

    actions.append((
        "warn",
        f"both {_rel(agents)} and {_rel(claude)} exist, but {_rel(claude)} does "
        f"not contain `@AGENTS.md`. Refusing to auto-edit — merge manually: "
        f"move CLAUDE.md content into AGENTS.md (or delete duplicates) and "
        f"add `@AGENTS.md` to CLAUDE.md.",
    ))


def _rel(p):
    """Format a path relative to cwd when possible, else absolute."""
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


def render(actions, dry_run):
    for entry in actions:
        kind, msg = entry[0], entry[1]
        prefix = {"ok": "  ✓", "change": "  +", "migrate": "  +",
                  "warn": "  ⚠", "error": "  ✗"}[kind]
        if kind in ("change", "migrate") and dry_run:
            print(f"{prefix} [dry-run] would {msg}")
        else:
            print(f"{prefix} {msg}")


def apply_actions(actions):
    """Run any callable apply_fn attached to a change/migrate entry."""
    for entry in actions:
        if entry[0] in ("change", "migrate") and len(entry) >= 3 and entry[2] is not None:
            entry[2]()


# --------------------------------------------------------------------------
# Recursive sweep: discover every directory carrying guidance, plan each.
# --------------------------------------------------------------------------
GUIDANCE_NAMES = ("CLAUDE.md", "AGENTS.md")


def _git_listed_files(root):
    """Repo-relative paths git knows about — tracked PLUS untracked-not-ignored
    (`--others --exclude-standard`). Honors the repo's own .gitignore, so
    vendored / generated trees are skipped for free. [] if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [p for p in out.stdout.split("\0") if p]


def _depth_key(d):
    return (len(d.parts), str(d))


def discover_targets(root, sweep_root):
    """Directories to plan: the sweep root itself (always — a fresh repo still
    bootstraps root guidance) plus every dir under it that already holds a
    CLAUDE.md or AGENTS.md. Absolute, deduped, shallow-first."""
    dirs = {sweep_root.resolve()}
    for rel in _git_listed_files(root):
        p = root / rel
        if p.name in GUIDANCE_NAMES:
            d = p.parent.resolve()
            if d == sweep_root.resolve() or _is_within(d, sweep_root):
                dirs.add(d)
    return sorted(dirs, key=_depth_key)


def _is_within(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def plan_all(targets):
    """plan() each discovered dir; return [(dir, actions), ...] in dir order."""
    results = []
    for d in targets:
        actions = []
        plan(d, actions)
        results.append((d, actions))
    return results


def wired_dirs_after(plan_results):
    """Dirs that will hold a content-bearing AGENTS.md once applied — every
    planned dir EXCEPT State-4 conflicts (warn) and broken installs (error),
    where CLAUDE.md keeps independent content the rewrite must not assume away."""
    wired = set()
    for d, actions in plan_results:
        kinds = {a[0] for a in actions}
        if kinds & {"warn", "error"}:
            continue
        wired.add(d)
    return wired


# --------------------------------------------------------------------------
# Cross-link normalization — keep the AGENTS.md graph internally coherent.
# --------------------------------------------------------------------------
# A path-shaped reference ending in CLAUDE.md (optional ./ or ../ prefix, then
# slash-separated segments). Whitespace-free. Bare `CLAUDE.md` matches too —
# syntactic context is what separates a real reference from a prose noun.
_PATHTOKEN = r"(?:\.{1,2}/)?(?:[\w.\-]+/)*CLAUDE\.md"

# The three contexts where such a token is a reference, not prose:
#   `]( TOK )`  inline-link destination
#   [ TOK ]     link text / reference label
#   ` TOK `     inline-code path
# A self-labeling link `[../x/CLAUDE.md](../x/CLAUDE.md)` flips both halves; a
# backticked label `[`../x/CLAUDE.md`](…)` flips via the code-span rule. The
# bare-noun false positive (`broken CLAUDE.md / README.md`) sits in none of
# these and is left untouched.
_CROSSLINK_RES = [
    re.compile(r"(?P<pre>\]\()(?P<tok>" + _PATHTOKEN + r")(?P<post>\))"),
    re.compile(r"(?P<pre>\[)(?P<tok>" + _PATHTOKEN + r")(?P<post>\])"),
    re.compile(r"(?P<pre>`)(?P<tok>" + _PATHTOKEN + r")(?P<post>`)"),
]

# Report-only signal: a path-qualified CLAUDE.md ref (a prefix or ≥1 named
# segment — so a bare `CLAUDE.md` noun is excluded) even when it is NOT inside
# link/code delimiters, to catch `// see src/CLAUDE.md "…"` in source comments.
_PATHREF_RE = re.compile(r"(?:\.{1,2}/|[\w.\-]+/)+CLAUDE\.md")


def _flip(tok):
    return tok[: -len("CLAUDE.md")] + "AGENTS.md"


def _target_dir(ref_dir, tok):
    """The directory a CLAUDE.md path token resolves to, relative to ref_dir."""
    return (ref_dir / tok).resolve().parent


def rewrite_text_crosslinks(text, file_dir, wired_dirs):
    """Flip path-shaped CLAUDE.md references whose target dir is wired →
    AGENTS.md, only inside link/code contexts. Returns (new_text, count)."""
    count = 0

    def repl(m):
        nonlocal count
        if _target_dir(file_dir, m.group("tok")) in wired_dirs:
            count += 1
            return m.group("pre") + _flip(m.group("tok")) + m.group("post")
        return m.group(0)

    for rx in _CROSSLINK_RES:
        text = rx.sub(repl, text)
    return text, count


def _predicted_agents_text(d, actions):
    """The AGENTS.md content a wired dir will hold after apply — computed from
    current files so the dry-run preview is exact even though migrations have
    not been written yet. None for a created-from-template dir (no cross-links)."""
    kinds = {a[0] for a in actions}
    if "migrate" in kinds:
        return retitle_claude_h1((d / "CLAUDE.md").read_text(encoding="utf-8"))
    agents = d / "AGENTS.md"
    if agents.exists():                       # State-4-ok: already content-bearing
        return agents.read_text(encoding="utf-8")
    return None                               # State-1/3 create: template only


def crosslink_changes(plan_results, wired_dirs):
    """[(agents_path, count, new_text), ...] for AGENTS.md files whose cross-links
    change. Drives both the dry-run preview and the apply write."""
    changes = []
    for d, actions in plan_results:
        if d not in wired_dirs:
            continue
        text = _predicted_agents_text(d, actions)
        if text is None:
            continue
        new_text, count = rewrite_text_crosslinks(text, d, wired_dirs)
        if count:
            changes.append((d / "AGENTS.md", count, new_text))
    return changes


def external_refs(root, sweep_root, wired_dirs):
    """Path-shaped CLAUDE.md references OUTSIDE the AGENTS.md graph (source
    comments, READMEs, .claude/agents, CI templates) that point at a now-wrapped
    CLAUDE.md. Report-only — the skill never edits these. [(rel, lineno, text)].

    A token is resolved BOTH file-relative (markdown-link convention) and
    repo-root-relative (source-comment convention, e.g. `// see src/CLAUDE.md`);
    a hit on either counts. The report is advisory, so the liberal match is
    deliberate — a missed reference is worse than a redundant report line."""
    hits = []
    for rel in _git_listed_files(root):
        p = root / rel
        if p.name in GUIDANCE_NAMES:          # AGENTS.md is auto-fixed; CLAUDE.md
            continue                          # is the wrapper / an unwired leaf
        if not _is_within(p.parent, sweep_root):
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue                          # binary / unreadable
        for i, line in enumerate(lines, 1):
            toks = {m.group(0) for m in _PATHREF_RE.finditer(line)}
            for rx in _CROSSLINK_RES:
                toks |= {m.group("tok") for m in rx.finditer(line)}
            if any(_target_dir(base, t) in wired_dirs
                   for t in toks for base in (p.parent, root)):
                hits.append((rel, i, line.strip()))
    return hits


def render_crosslinks(changes, dry_run):
    if not changes:
        return
    print("\nCross-link normalization (AGENTS.md graph):")
    for agents, count, _ in changes:
        verb = "would flip" if dry_run else "flipped"
        plural = "ref" if count == 1 else "refs"
        print(f"  + {verb} {count} {plural} to AGENTS.md in {_rel(agents)}")


def render_external(hits):
    print("\nExternal CLAUDE.md references (report only — not edited):")
    if not hits:
        print("  ✓ none point at a now-wrapped CLAUDE.md")
        return
    for rel, lineno, text in hits:
        print(f"  ⓘ {rel}:{lineno}: {text}")
    print("  ↳ these resolve to the CLAUDE.md wrapper, not the content; "
          "update by hand if Codex/antigravity should follow them to AGENTS.md.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=None,
                    help="sweep root (defaults to git root). Pass a subdir like "
                         "`src` to scope the recursive sweep to that subtree.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report planned changes without writing anything")
    ap.add_argument("--single", action="store_true",
                    help="operate on exactly PATH (no recursion) — the "
                         "pre-recursive single-directory behavior")
    args = ap.parse_args()

    root = find_repo_root()
    if root is None:
        print("ERROR: not inside a git repository (git rev-parse --show-toplevel failed).",
              file=sys.stderr)
        return 1

    if args.path is None:
        sweep_root = root
    else:
        sweep_root = Path(args.path).resolve()
        try:
            sweep_root.relative_to(root)
        except ValueError:
            print(f"ERROR: {sweep_root} is not inside the repo at {root}.", file=sys.stderr)
            return 1
        if not sweep_root.is_dir():
            print(f"ERROR: not a directory: {sweep_root}", file=sys.stderr)
            return 1

    targets = [sweep_root.resolve()] if args.single \
        else discover_targets(root, sweep_root)
    plan_results = plan_all(targets)

    if any(a[0] == "error" for _, actions in plan_results for a in actions):
        for _, actions in plan_results:
            for a in actions:
                if a[0] == "error":
                    print(f"ERROR: {a[1]}", file=sys.stderr)
        return 1

    scope = "single dir" if args.single else "recursive sweep from"
    print(f"AGENTS.md ↔ CLAUDE.md — {scope} {_rel(sweep_root)}")
    for d, actions in plan_results:
        # Quiet the already-wired dirs in a multi-dir sweep; show them in single mode.
        if not args.single and all(a[0] == "ok" for a in actions):
            continue
        print(f"\n{_rel(d)}/")
        render(actions, args.dry_run)

    wired = wired_dirs_after(plan_results)
    changes = crosslink_changes(plan_results, wired)
    hits = external_refs(root, sweep_root, wired)

    render_crosslinks(changes, args.dry_run)
    render_external(hits)

    any_pending = any(a[0] in ("change", "migrate")
                      for _, actions in plan_results for a in actions)
    any_warn = any(a[0] == "warn" for _, actions in plan_results for a in actions)

    print()
    if args.dry_run:
        if any_pending or changes:
            print("Dry run only — re-run without --dry-run to apply.")
            return 0
        if any_warn:
            print("Nothing to apply — review the ⚠ line(s) above.")
            return 2
        print("Already wired up — nothing to do.")
        return 0

    for _, actions in plan_results:
        apply_actions(actions)
    for agents, _, new_text in changes:
        agents.write_text(new_text, encoding="utf-8")

    if any_pending or changes:
        print("AGENTS.md ↔ CLAUDE.md relationship wired up.")
    elif any_warn:
        print("Nothing applied — review the ⚠ line(s) above.")
        return 2
    else:
        print("Already wired up — nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
