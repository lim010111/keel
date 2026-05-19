#!/usr/bin/env python3
"""SessionEnd hook - auto-generate a Korean dev log for meaningful sessions.

On session end, decide whether the session was substantive enough to deserve a
dev log. A session counts as meaningful when it has >= 2 *meaningful* user
turns, where a meaningful turn is a real prompt, or a slash command whose
arguments are themselves the request (e.g. `/grill-me <long text>`). Bare
commands like `/clear` or `/usage` carry no real content and do not count.

If meaningful, spawn a fully detached headless `claude` that delegates to the
`korean-context-writer` subagent, which runs the `session-dev-log` skill to
write the 티키타카 markdown log into the Obsidian vault.

Dedup: a session can end many times over its life (every `/resume` ends it
again). Re-running Opus each time - especially when the user just opened an
old session and left without doing anything - is pure waste. So the headless
run records, in a per-session marker file, the meaningful-turn count it
generated at and the path it wrote. A later SessionEnd regenerates only when
the count has actually grown. Because the dev-log filename embeds an
AI-chosen title that can drift between runs, the marker also carries the old
path so a regeneration can delete the now-stale file instead of orphaning it.
The marker is written by the headless run (not here): if that run fails, no
marker is left and the next SessionEnd retries.

The hook never blocks: the cheap, deterministic gating runs synchronously here
and the slow Opus work is handed to a detached background process
(start_new_session) so it survives the parent session's exit.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
COLLECT = CLAUDE_DIR / "skills" / "session-dev-log" / "scripts" / "collect_session.py"
LOG_DIR = CLAUDE_DIR / "hooks" / ".session-devlog"
MARKER_DIR = LOG_DIR / "markers"
GUARD_ENV = "CLAUDE_DEVLOG_HOOK"

MIN_MEANINGFUL_TURNS = 2
MIN_COMMAND_ARG_CHARS = 10   # non-space arg chars for a command turn to count


def log(msg: str) -> None:
    """Append one line to the shared hook log (best-effort)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        with open(LOG_DIR / "hook.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def meaningful_turn_count(collect_json: dict) -> int:
    """Count human turns that carry real content.

    `prompt` turns always count. `command` turns count only when their
    arguments are substantial - `collect_session.py` emits command text as
    "/cmd args...", so the first token is dropped and the rest measured.
    """
    n = 0
    for turn in collect_json.get("turns", []):
        if turn.get("role") != "human":
            continue
        kind = turn.get("kind")
        text = turn.get("text", "") or ""
        if kind == "prompt":
            n += 1
        elif kind == "command":
            parts = text.split(None, 1)
            args = parts[1] if len(parts) > 1 else ""
            if len("".join(args.split())) > MIN_COMMAND_ARG_CHARS:
                n += 1
    return n


def read_marker(session_id: str) -> tuple[int, str | None]:
    """Return (count, path) recorded at the last dev-log generation.

    (-1, None) when no usable marker exists. A marker is JSON
    {"count": N, "path": "..."}; a bare integer (legacy / hand-written) is
    accepted too, and anything unparseable is treated as absent.
    """
    try:
        data = json.loads((MARKER_DIR / session_id).read_text().strip())
        if isinstance(data, dict):
            return int(data.get("count", -1)), data.get("path")
        if isinstance(data, (int, float)):
            return int(data), None
    except Exception:
        pass
    return -1, None


def main() -> None:
    # Recursion guard: the headless claude we spawn also fires SessionEnd.
    if os.environ.get(GUARD_ENV):
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    transcript = str(payload.get("transcript_path", ""))
    if not transcript or not Path(transcript).is_file():
        log(f"skip: no transcript ({payload.get('session_id', '')})")
        sys.exit(0)

    # session_id keys the dedup marker; fall back to the JSONL stem, which IS
    # the session id, if the payload somehow omits it.
    session_id = str(payload.get("session_id", "")) or Path(transcript).stem

    # Cheap, deterministic gating - reuse the skill's own collector.
    try:
        proc = subprocess.run(
            [sys.executable, str(COLLECT), "--file", transcript],
            capture_output=True, text=True, timeout=120,
        )
        collect_json = json.loads(proc.stdout)
    except Exception as e:
        log(f"skip: collect failed ({session_id}): {e}")
        sys.exit(0)

    turns = meaningful_turn_count(collect_json)
    if turns < MIN_MEANINGFUL_TURNS:
        log(f"skip: {turns} meaningful turn(s) ({session_id})")
        sys.exit(0)

    # Dedup: only regenerate when the session gained meaningful turns since the
    # last dev log. Catches "opened an old session and left without doing
    # anything" - the whole transcript is meaningful but nothing is new.
    last_count, last_path = read_marker(session_id)
    if turns <= last_count:
        log(f"skip: no new meaningful turns (now={turns}, last={last_count}) "
            f"({session_id})")
        sys.exit(0)

    claude = shutil.which("claude")
    if not claude:
        log(f"skip: claude not on PATH ({session_id})")
        sys.exit(0)

    marker_file = MARKER_DIR / session_id

    # Tell the headless run to delete the previous file only when one is known
    # - its title (and thus filename) may have drifted as the session grew.
    prev_line = ""
    if last_path:
        prev_line = (
            f"- 이 세션의 이전 dev log 파일이 `{last_path}` 에 있다. 새로 저장한 "
            "파일 경로가 이 경로와 다르면(제목이 바뀐 경우) 이전 파일을 삭제하라.\n"
        )

    prompt = (
        "이 작업은 방금 종료된 Claude Code 세션의 개발 로그를 자동 생성하는 "
        "백그라운드 작업이다.\n\n"
        "`Agent` 툴로 `korean-context-writer` 서브에이전트를 실행하고, 그 "
        "서브에이전트가 `session-dev-log` 스킬을 사용해 아래 세션의 티키타카 "
        "개발 로그를 작성하게 하라.\n\n"
        f"- 대상 세션 transcript 파일: {transcript}\n"
        "- collect_session.py를 실행할 때 반드시 `--file <위 경로>` 옵션을 "
        "사용할 것. 기본 '최근 JSONL 추측' 로직을 쓰면 이 백그라운드 세션 "
        "자신을 정리하게 되므로 절대 금지.\n"
        "- 저장 경로에 같은 이름 파일이 이미 있으면 사용자에게 묻지 말고 그대로 "
        "덮어쓸 것 (헤드리스 실행이라 응답할 사용자가 없음).\n"
        + prev_line +
        "- 모든 작업이 끝나면 마지막으로 마커 파일 `" + str(marker_file) + "` 에 "
        "정확히 다음 JSON 한 줄만 기록하라(기존 내용 덮어쓰기): "
        '{"count": ' + str(turns) + ', "path": "<방금 저장한 dev log의 절대 경로>"}\n'
        "- 완료되면 저장한 절대 경로만 출력."
    )

    env = dict(os.environ)
    env[GUARD_ENV] = "1"

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        MARKER_DIR.mkdir(parents=True, exist_ok=True)  # headless writes here
        run_log = open(LOG_DIR / f"{session_id}.log", "a", encoding="utf-8")
        run_log.write(
            f"\n=== {datetime.now().isoformat(timespec='seconds')} "
            f"devlog run (turns={turns}, last={last_count}) ===\n"
        )
        run_log.flush()
        subprocess.Popen(
            [claude, "-p", prompt, "--model", "sonnet",
             "--dangerously-skip-permissions"],
            stdin=subprocess.DEVNULL, stdout=run_log, stderr=run_log,
            start_new_session=True, env=env,
        )
    except Exception as e:
        log(f"skip: spawn failed ({session_id}): {e}")
        sys.exit(0)

    # The marker is written by the headless run on success, not here - so a
    # failed run leaves no marker and the next SessionEnd retries.
    log(f"spawned devlog run: turns={turns}, last={last_count} ({session_id})")
    sys.exit(0)


if __name__ == "__main__":
    main()
