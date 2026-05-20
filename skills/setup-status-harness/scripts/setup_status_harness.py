#!/usr/bin/env python3
"""Idempotently install the STATUS harness in two layers.

Global layer (always attempted):
  1. ~/.claude/scripts/status.py — the generator. Copied from this skill's
     bundled snapshot when missing; an already-present script is untouched.
  2. ~/.claude/settings.json — a SessionStart hook (runs status.py + prints
     STATUS.md) and a Stop hook (runs status.py).

Project layer (attempted only when cwd is inside a git repo, unless --no-project):
  3. <repo>/scripts/status.py — vendored copy so GitHub Actions runners
     (which cannot reach ~/.claude) can run the generator. Vendored from the
     global copy if present, else the bundled snapshot.
  4. <repo>/.github/workflows/regen-status.yml — regenerates STATUS.md after
     each push to main, so worktree branches do not need to commit it.
  5. <repo>/.gitignore — adds .claude/handoffs/ and .claude/worktrees/.

Idempotent. Every step reports ✓ (already in place), + (will change), or
⚠ (present but differs from template — manual review). Re-running after a
successful apply is a no-op.

Usage:
  setup_status_harness.py --dry-run     # preview, write nothing
  setup_status_harness.py               # apply both layers
  setup_status_harness.py --no-project  # global layer only
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

CLAUDE = Path.home() / ".claude"
SCRIPTS = CLAUDE / "scripts"
SETTINGS = CLAUDE / "settings.json"
STATUS_PY = SCRIPTS / "status.py"
BUNDLED = Path(__file__).resolve().parent / "status.py"

# $HOME (not an absolute /home/<user> path) keeps hooks portable across
# machines. Hooks run in a shell, so $HOME expands at run time.
SESSIONSTART_CMD = (
    'root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '
    'python3 "$HOME/.claude/scripts/status.py" >/dev/null 2>&1; '
    'if [ -f "$root/STATUS.md" ]; then '
    "echo '=== Project status board (STATUS.md) ==='; "
    'cat "$root/STATUS.md"; fi'
)
STOP_CMD = 'python3 "$HOME/.claude/scripts/status.py"'

WORKFLOW_REL = Path(".github") / "workflows" / "regen-status.yml"
WORKFLOW_CONTENT = """name: Regen STATUS.md

on:
  push:
    branches: [main]
    paths:
      - '.scratch/**'
      - 'scripts/status.py'

concurrency:
  group: regen-status-${{ github.ref }}
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  regen:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.x'
      - run: python3 scripts/status.py
      - name: Commit if changed
        run: |
          if ! git diff --quiet STATUS.md; then
            git config user.name "github-actions[bot]"
            git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
            git add STATUS.md
            git commit -m "Regenerate STATUS.md"
            git push
          fi
"""

GITIGNORE_HEADER = "# Claude Code session-local state (per-worktree; not for sharing)"
GITIGNORE_ENTRIES = [".claude/handoffs/", ".claude/worktrees/"]


def hook_has(groups, needle):
    """True if any hook command across `groups` contains `needle`."""
    for group in groups:
        for h in group.get("hooks", []):
            if needle in h.get("command", ""):
                return True
    return False


def find_repo_root():
    """Path to git toplevel, or None if cwd is not in a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def install_global(actions, apply):
    # status.py
    if STATUS_PY.exists():
        actions.append(("ok", f"status.py present at {STATUS_PY}"))
    elif not BUNDLED.exists():
        print(f"ERROR: bundled snapshot missing at {BUNDLED}", file=sys.stderr)
        return False
    else:
        actions.append(("change", f"install status.py -> {STATUS_PY}"))
        if apply:
            SCRIPTS.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BUNDLED, STATUS_PY)

    # settings.json hooks
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"ERROR: {SETTINGS} is not valid JSON ({e}); fix it first.",
                  file=sys.stderr)
            return False
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    changed = False

    ss = hooks.setdefault("SessionStart", [])
    if hook_has(ss, "status.py"):
        actions.append(("ok", "SessionStart hook already runs status.py"))
    else:
        actions.append(("change", "add SessionStart hook "
                                   "(runs status.py, prints STATUS.md)"))
        ss.append({"hooks": [{"type": "command", "command": SESSIONSTART_CMD}]})
        changed = True

    st = hooks.setdefault("Stop", [])
    if hook_has(st, "status.py"):
        actions.append(("ok", "Stop hook already runs status.py"))
    else:
        actions.append(("change", "add Stop hook (runs status.py)"))
        st.append({"hooks": [{"type": "command", "command": STOP_CMD}]})
        changed = True

    if changed and apply:
        CLAUDE.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n",
                            encoding="utf-8")
    return True


def install_project(root, actions, apply):
    # Source for the vendored script: prefer the global copy (which may be
    # newer than the bundled snapshot); fall back to bundled otherwise.
    source = STATUS_PY if STATUS_PY.exists() else BUNDLED
    source_text = source.read_text(encoding="utf-8")

    # Vendored status.py
    vendored = root / "scripts" / "status.py"
    if vendored.exists():
        if vendored.read_text(encoding="utf-8") == source_text:
            actions.append(("ok", f"scripts/status.py in sync with {source}"))
        else:
            actions.append(("warn", f"scripts/status.py differs from {source} — "
                                     "review and copy manually if you want to sync"))
    else:
        actions.append(("change", f"vendor scripts/status.py from {source}"))
        if apply:
            vendored.parent.mkdir(parents=True, exist_ok=True)
            vendored.write_text(source_text, encoding="utf-8")

    # CI workflow
    wf = root / WORKFLOW_REL
    if wf.exists():
        if wf.read_text(encoding="utf-8") == WORKFLOW_CONTENT:
            actions.append(("ok", f"{WORKFLOW_REL} present"))
        else:
            actions.append(("warn", f"{WORKFLOW_REL} present but differs from template"))
    else:
        actions.append(("change", f"create {WORKFLOW_REL}"))
        if apply:
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(WORKFLOW_CONTENT, encoding="utf-8")

    # .gitignore entries (additive: only appends missing lines, never modifies
    # existing ones).
    gi = root / ".gitignore"
    existing_text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    existing_lines = {line.strip() for line in existing_text.splitlines()}
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing_lines]

    if not missing:
        actions.append(("ok", ".gitignore covers .claude/handoffs and .claude/worktrees"))
    else:
        actions.append(("change", f"append {len(missing)} entry/entries to .gitignore"))
        if apply:
            addition = ""
            if existing_text and not existing_text.endswith("\n"):
                addition += "\n"
            if existing_text.strip():
                addition += "\n"
            addition += GITIGNORE_HEADER + "\n"
            for entry in missing:
                addition += entry + "\n"
            with gi.open("a", encoding="utf-8") as f:
                f.write(addition)


def render(label, actions, dry_run):
    print(label)
    for kind, msg in actions:
        prefix = {"ok": "  ✓", "change": "  +", "warn": "  ⚠"}[kind]
        if kind == "change" and dry_run:
            print(f"{prefix} [dry-run] would {msg}")
        else:
            print(f"{prefix} {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report planned changes without writing anything")
    ap.add_argument("--no-project", action="store_true",
                    help="install the global layer only; skip per-project files")
    args = ap.parse_args()

    apply = not args.dry_run
    global_actions = []
    if not install_global(global_actions, apply):
        return 1
    render("Global layer:", global_actions, args.dry_run)

    project_actions = []
    root = None
    if args.no_project:
        print("\nProject layer:\n  (skipped — --no-project)")
    else:
        root = find_repo_root()
        if root is None:
            print("\nProject layer:\n  (skipped — cwd is not inside a git repo)")
        else:
            install_project(root, project_actions, apply)
            render(f"\nProject layer — {root}:", project_actions, args.dry_run)

    pending = any(k == "change" for k, _ in global_actions + project_actions)
    warnings = any(k == "warn" for k, _ in global_actions + project_actions)
    print()
    if pending and args.dry_run:
        print("Dry run only — re-run without --dry-run to apply.")
    elif pending:
        print("Harness installed.")
    elif warnings:
        print("Harness already in place; review the ⚠ line(s) above.")
    else:
        print("Harness already complete — nothing to do.")
        wf_was_created = any(
            k == "change" and "regen-status.yml" in msg for k, msg in project_actions
        )
        if wf_was_created:
            print("\nNext step: in GitHub → repo Settings → Actions → General → "
                  "Workflow permissions, enable 'Read and write permissions' so "
                  "the regen-status workflow can push STATUS.md updates back to main.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
