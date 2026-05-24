#!/usr/bin/env python3
"""Bootstrap the AGENTS.md ↔ CLAUDE.md relationship in a repo or subdir.

AGENTS.md is the canonical source — Codex CLI and antigravity CLI both read
it directly, the same way Claude reads CLAUDE.md. CLAUDE.md `@import`s
AGENTS.md so the two stay in sync without duplicating content.

This script only sets up the *relationship*; authoring the content of
AGENTS.md is the project's job.

States handled at the target directory:
  1. Neither file        → create both (AGENTS.md from template, CLAUDE.md @import wrapper)
  2. Only CLAUDE.md      → move CLAUDE.md content into AGENTS.md, replace
                           CLAUDE.md with @import wrapper (git holds the
                           original, so no .bak file)
  3. Only AGENTS.md      → create CLAUDE.md @import wrapper
  4. Both files          → if CLAUDE.md already imports AGENTS.md, silent
                           no-op. Else refuse: print ⚠ and let the human
                           decide how to merge.

Target directory defaults to `git rev-parse --show-toplevel`. Pass a
positional path to operate on a subdirectory (e.g. `src/auth`) — this
covers the "AGENTS.md may live under src/<context>/" expansion convention.

Idempotent: every state's wired-up path reports ✓ and writes nothing on
rerun. Output uses the same ✓ / + / ⚠ markers as setup-status-harness.

Usage:
  setup_agents_md.py [--dry-run] [PATH]
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


def plan(target, actions):
    """Append (kind, msg, apply_fn) tuples describing what to do at `target`."""
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
            agents.write_text(claude_text, encoding="utf-8")
            claude.write_text(claude_tpl, encoding="utf-8")
        actions.append((
            "change",
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
        prefix = {"ok": "  ✓", "change": "  +", "warn": "  ⚠", "error": "  ✗"}[kind]
        if kind == "change" and dry_run:
            print(f"{prefix} [dry-run] would {msg}")
        else:
            print(f"{prefix} {msg}")


def apply_actions(actions):
    """Run any callable apply_fn attached to a change entry."""
    for entry in actions:
        if entry[0] == "change" and len(entry) >= 3 and entry[2] is not None:
            entry[2]()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=None,
                    help="directory to set up (defaults to git root). Pass a subdir "
                         "like `src/auth` to wire up nested AGENTS.md/CLAUDE.md.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report planned changes without writing anything")
    args = ap.parse_args()

    root = find_repo_root()
    if root is None:
        print("ERROR: not inside a git repository (git rev-parse --show-toplevel failed).",
              file=sys.stderr)
        return 1

    if args.path is None:
        target = root
    else:
        target = Path(args.path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            print(f"ERROR: {target} is not inside the repo at {root}.", file=sys.stderr)
            return 1
        if not target.exists():
            print(f"ERROR: target directory does not exist: {target}", file=sys.stderr)
            return 1
        if not target.is_dir():
            print(f"ERROR: target is not a directory: {target}", file=sys.stderr)
            return 1

    actions = []
    plan(target, actions)

    if any(k == "error" for k, *_ in actions):
        for entry in actions:
            if entry[0] == "error":
                print(f"ERROR: {entry[1]}", file=sys.stderr)
        return 1

    print(f"AGENTS.md ↔ CLAUDE.md setup — target: {_rel(target)}")
    render(actions, args.dry_run)

    pending = any(k == "change" for k, *_ in actions)
    warnings = any(k == "warn" for k, *_ in actions)

    print()
    if pending and args.dry_run:
        print("Dry run only — re-run without --dry-run to apply.")
    elif pending:
        apply_actions(actions)
        print("AGENTS.md ↔ CLAUDE.md relationship wired up.")
    elif warnings:
        print("Nothing applied — review the ⚠ line(s) above.")
        return 2
    else:
        print("Already wired up — nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
