#!/usr/bin/env python3
"""Idempotently install the STATUS harness global infrastructure.

Ensures two things, then stops — issue creation and the narrative are out of
scope (see SKILL.md):

  1. ~/.claude/scripts/status.py exists. Copied from this skill's bundled
     snapshot when missing. An already-present script is left untouched.
  2. ~/.claude/settings.json has a SessionStart hook that runs status.py and
     prints STATUS.md, and a Stop hook that runs status.py.

Idempotent: once both are in place, re-running changes nothing and reports
all-✓. Detection is by the substring `status.py` in a hook command, so an
existing harness hook is recognised regardless of its exact wording, and a
project's own unrelated SessionStart/Stop hooks are never disturbed.

Usage:
  setup_status_harness.py --dry-run   # preview, write nothing
  setup_status_harness.py             # apply
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

CLAUDE = Path.home() / ".claude"
SCRIPTS = CLAUDE / "scripts"
SETTINGS = CLAUDE / "settings.json"
STATUS_PY = SCRIPTS / "status.py"
BUNDLED = Path(__file__).resolve().parent / "status.py"

# $HOME (not an absolute /home/<user> path) keeps the hook portable across
# machines. Hooks run in a shell, so $HOME expands at run time.
SESSIONSTART_CMD = (
    'root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; '
    'python3 "$HOME/.claude/scripts/status.py" >/dev/null 2>&1; '
    'if [ -f "$root/STATUS.md" ]; then '
    "echo '=== Project status board (STATUS.md) ==='; "
    'cat "$root/STATUS.md"; fi'
)
STOP_CMD = 'python3 "$HOME/.claude/scripts/status.py"'


def hook_has(groups, needle):
    """True if any hook command across `groups` contains `needle`."""
    for group in groups:
        for h in group.get("hooks", []):
            if needle in h.get("command", ""):
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report planned changes without writing anything")
    args = ap.parse_args()

    actions = []  # (kind, message): kind in {"ok", "change"}

    # 1. status.py ----------------------------------------------------------
    if STATUS_PY.exists():
        actions.append(("ok", f"status.py present at {STATUS_PY}"))
    elif not BUNDLED.exists():
        print(f"ERROR: bundled snapshot missing at {BUNDLED}", file=sys.stderr)
        return 1
    else:
        actions.append(("change", f"install status.py -> {STATUS_PY}"))
        if not args.dry_run:
            SCRIPTS.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BUNDLED, STATUS_PY)

    # 2. settings.json hooks ------------------------------------------------
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"ERROR: {SETTINGS} is not valid JSON ({e}); fix it first.",
                  file=sys.stderr)
            return 1
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

    if changed and not args.dry_run:
        CLAUDE.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n",
                            encoding="utf-8")

    # report ----------------------------------------------------------------
    for kind, msg in actions:
        if kind == "ok":
            print(f"  ✓ {msg}")
        else:
            print(f"  + {'[dry-run] would ' if args.dry_run else ''}{msg}")

    pending = any(k == "change" for k, _ in actions)
    if not pending:
        print("\nHarness infrastructure already complete — nothing to do.")
    elif args.dry_run:
        print("\nDry run only — re-run without --dry-run to apply.")
    else:
        print("\nHarness infrastructure installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
