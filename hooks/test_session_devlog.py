#!/usr/bin/env python3
"""Verification suite for the SessionEnd dev-log hook (session_devlog.py).

Stdlib `unittest` only - pytest is not installed in this environment.
Run:  python3 hooks/test_session_devlog.py -v

The hook's slow path spawns a headless `claude`. Tests never invoke the real
binary: a fake `claude` on a temp PATH records its argv to a sentinel file
instead, so the spawn *decision*, the dedup gating, and the handoff *contract*
(what the prompt instructs the headless run to do) are all verified end-to-end
without cost. The marker file itself is written by the real headless run, so
tests assert the hook never writes it and that the prompt carries everything
that run needs (marker path, turn count, previous file path).

Isolation: every test uses a synthetic `DEVLOGTEST-<uuid>` session id and
cleans its own marker / per-session log. setUp/tearDownModule snapshot
hook.log and assert no real (non-`DEVLOGTEST-`) marker was touched.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
HOOK = HOOKS / "session_devlog.py"
LOG_DIR = Path.home() / ".claude" / "hooks" / ".session-devlog"
MARKER_DIR = LOG_DIR / "markers"
HOOK_LOG = LOG_DIR / "hook.log"

sys.path.insert(0, str(HOOKS))
import session_devlog as sd  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def make_transcript(path: Path, turns) -> None:
    """Write a minimal Claude Code session JSONL.

    `turns` is a list of ("prompt", text) | ("command", "name args") tuples.
    An assistant line is interleaved after each so the file looks realistic.
    """
    lines = []
    for i, (kind, body) in enumerate(turns):
        if kind == "prompt":
            content = body
        elif kind == "command":
            name, _, args = body.partition(" ")
            content = (f"<command-name>{name}</command-name>"
                       f"<command-args>{args}</command-args>")
        else:
            raise ValueError(kind)
        lines.append(json.dumps({
            "type": "user",
            "timestamp": f"2026-05-18T0{i}:00:00Z",
            "cwd": "/tmp/proj",
            "message": {"role": "user", "content": content},
        }))
        lines.append(json.dumps({
            "type": "assistant",
            "timestamp": f"2026-05-18T0{i}:00:05Z",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "응답"}]},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


P = ("prompt", "이것은 의미 있는 실제 사용자 질문입니다")
LONG_CMD = ("command", "grill-me 이건 인자가 충분히 긴 실제 요청 내용이다")


_SNAPSHOT = {}


def _markers():
    return sorted(p.name for p in MARKER_DIR.iterdir()) if MARKER_DIR.exists() else []


def setUpModule():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT["hook_log"] = HOOK_LOG.read_bytes() if HOOK_LOG.exists() else None
    _SNAPSHOT["markers"] = [n for n in _markers() if not n.startswith("DEVLOGTEST-")]


def tearDownModule():
    # Restore hook.log so test runs leave no trace.
    if _SNAPSHOT["hook_log"] is None:
        HOOK_LOG.unlink(missing_ok=True)
    else:
        HOOK_LOG.write_bytes(_SNAPSHOT["hook_log"])
    # Safety gate: no real marker may have been added/removed/clobbered.
    after = [n for n in _markers() if not n.startswith("DEVLOGTEST-")]
    assert after == _SNAPSHOT["markers"], (
        "real markers changed!", _SNAPSHOT["markers"], after)


class DevLogHookTest(unittest.TestCase):
    def setUp(self):
        self.sid = f"DEVLOGTEST-{uuid.uuid4()}"
        self.tmp = Path(tempfile.mkdtemp(prefix="devlog-test-"))
        # fake `claude`: records its full argv to a sentinel, then exits.
        self.sentinel = self.tmp / "claude_argv"
        fake = self.tmp / "claude"
        fake.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" > "{self.sentinel}"\n'
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.transcript = self.tmp / f"{self.sid}.jsonl"

    def tearDown(self):
        (MARKER_DIR / self.sid).unlink(missing_ok=True)
        (LOG_DIR / f"{self.sid}.log").unlink(missing_ok=True)
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        self.tmp.rmdir()

    # -- helpers -----------------------------------------------------------
    def run_hook(self, payload, *, guard=False, with_fake_claude=True):
        env = os.environ.copy()
        if with_fake_claude:
            env["PATH"] = f"{self.tmp}{os.pathsep}{env['PATH']}"
        else:
            # empty PATH so `claude` is undiscoverable; the hook is still
            # launched via an absolute python, and runs collect_session.py
            # via sys.executable, so neither needs PATH.
            empty = self.tmp / "emptybin"
            empty.mkdir(exist_ok=True)
            env["PATH"] = str(empty)
        if guard:
            env[sd.GUARD_ENV] = "1"
        else:
            env.pop(sd.GUARD_ENV, None)
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=60,
        )

    def spawned(self, wait=4.0):
        """Poll for the detached fake-claude sentinel."""
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.sentinel.exists():
                return True
            time.sleep(0.05)
        return False

    def prompt(self):
        """The full argv the hook handed the (fake) claude, as one string."""
        return self.sentinel.read_text() if self.sentinel.exists() else ""

    def payload(self, **over):
        p = {"session_id": self.sid, "hook_event_name": "SessionEnd",
             "reason": "prompt_input_exit", "cwd": "/tmp/proj",
             "transcript_path": str(self.transcript)}
        p.update(over)
        return p

    def marker_raw(self):
        f = MARKER_DIR / self.sid
        return f.read_text() if f.exists() else None

    # -- unit: meaningful_turn_count ---------------------------------------
    def test_count_prompts(self):
        cj = {"turns": [{"role": "human", "kind": "prompt", "text": "a"},
                        {"role": "human", "kind": "prompt", "text": "b"},
                        {"role": "ai", "text": "x"}]}
        self.assertEqual(sd.meaningful_turn_count(cj), 2)

    def test_count_bare_commands_excluded(self):
        cj = {"turns": [{"role": "human", "kind": "command", "text": "/clear"},
                        {"role": "human", "kind": "command", "text": "/usage"}]}
        self.assertEqual(sd.meaningful_turn_count(cj), 0)

    def test_count_command_with_args_included(self):
        cj = {"turns": [{"role": "human", "kind": "command",
                         "text": "/grill-me 충분히 긴 실제 요청 내용입니다"}]}
        self.assertEqual(sd.meaningful_turn_count(cj), 1)

    # -- unit: read_marker -------------------------------------------------
    def test_read_marker_json(self):
        (MARKER_DIR / self.sid).write_text('{"count": 7, "path": "/a/b.md"}')
        self.assertEqual(sd.read_marker(self.sid), (7, "/a/b.md"))

    def test_read_marker_legacy_int(self):
        (MARKER_DIR / self.sid).write_text("5")
        self.assertEqual(sd.read_marker(self.sid), (5, None))

    def test_read_marker_corrupt(self):
        (MARKER_DIR / self.sid).write_text("garbage!!")
        self.assertEqual(sd.read_marker(self.sid), (-1, None))

    def test_read_marker_missing(self):
        self.assertEqual(sd.read_marker(self.sid), (-1, None))

    # -- error / guard paths ----------------------------------------------
    def test_recursion_guard(self):
        make_transcript(self.transcript, [P, P, P])
        r = self.run_hook(self.payload(), guard=True)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.spawned(wait=1.5), "guarded run must not spawn")

    def test_malformed_stdin(self):
        r = subprocess.run([sys.executable, str(HOOK)], input="not json{",
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)

    def test_missing_transcript_path(self):
        r = self.run_hook({"session_id": self.sid})
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.spawned(wait=1.5))

    def test_nonexistent_transcript(self):
        r = self.run_hook(self.payload(transcript_path="/no/such/file.jsonl"))
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.spawned(wait=1.5))

    # -- gating ------------------------------------------------------------
    def test_below_threshold_one_turn(self):
        make_transcript(self.transcript, [P])
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.spawned(wait=1.5), "1 turn must not spawn")
        self.assertIsNone(self.marker_raw())

    def test_bare_commands_only(self):
        make_transcript(self.transcript,
                        [("command", "clear"), ("command", "usage")])
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.spawned(wait=1.5))
        self.assertIsNone(self.marker_raw())

    def test_no_claude_on_path(self):
        make_transcript(self.transcript, [P, P, P])
        r = self.run_hook(self.payload(), with_fake_claude=False)
        self.assertEqual(r.returncode, 0)
        self.assertIsNone(self.marker_raw())

    # -- spawn + handoff contract -----------------------------------------
    def test_meaningful_session_spawns(self):
        make_transcript(self.transcript, [P, P, P])
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.spawned(), "3-turn session must spawn")
        p = self.prompt()
        self.assertIn("korean-context-writer", p)
        self.assertIn("session-dev-log", p)
        self.assertIn(str(self.transcript), p)
        self.assertIn("--file", p)
        self.assertIn(str(MARKER_DIR / self.sid), p, "marker path handed off")
        self.assertIn('"count": 3', p, "turn count handed off")
        self.assertIsNone(self.marker_raw(),
                          "hook must not write the marker - the headless run does")

    def test_command_with_args_counts_toward_threshold(self):
        make_transcript(self.transcript, [P, LONG_CMD])
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.spawned(), "prompt + arg-command = 2 -> spawn")
        self.assertIn('"count": 2', self.prompt())

    def test_first_run_prompt_omits_old_path(self):
        make_transcript(self.transcript, [P, P, P])
        self.run_hook(self.payload())
        self.assertTrue(self.spawned())
        self.assertNotIn("이전 파일을 삭제", self.prompt(),
                         "no previous file -> no delete instruction")

    # -- dedup -------------------------------------------------------------
    def test_dedup_resume_and_leave_skipped(self):
        """Marker count == current count -> nothing new -> skip (the core fix)."""
        make_transcript(self.transcript, [P, P, P])
        marker = '{"count": 3, "path": "/vault/x.md"}'
        (MARKER_DIR / self.sid).write_text(marker)
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.spawned(wait=1.5),
                         "unchanged transcript must not regenerate")
        self.assertEqual(self.marker_raw(), marker, "marker untouched")

    def test_dedup_regenerates_on_growth(self):
        """Grown past the marker -> regenerate; hand off old path for cleanup."""
        make_transcript(self.transcript, [P, P, P, P, P])
        marker = '{"count": 3, "path": "/vault/old-stale-title.md"}'
        (MARKER_DIR / self.sid).write_text(marker)
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.spawned(), "grown session must regenerate")
        p = self.prompt()
        self.assertIn("/vault/old-stale-title.md", p, "old path handed off")
        self.assertIn("이전 파일을 삭제", p, "delete-stale instruction present")
        self.assertIn('"count": 5', p, "new count handed off")
        self.assertEqual(self.marker_raw(), marker,
                         "hook must not touch the marker")

    def test_dedup_legacy_int_marker_gates(self):
        make_transcript(self.transcript, [P, P, P])
        (MARKER_DIR / self.sid).write_text("3")   # legacy bare int, == 3 turns
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.spawned(wait=1.5), "int marker 3 >= 3 turns -> skip")

    def test_dedup_corrupt_marker_regenerates(self):
        make_transcript(self.transcript, [P, P, P])
        (MARKER_DIR / self.sid).write_text("garbage!!")
        r = self.run_hook(self.payload())
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.spawned(), "corrupt marker -> treat as none")

    # -- fallbacks ---------------------------------------------------------
    def test_session_id_falls_back_to_transcript_stem(self):
        make_transcript(self.transcript, [P, P, P])
        r = self.run_hook(self.payload(session_id=""))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(self.spawned())
        # marker is keyed by the JSONL stem (== self.sid here)
        self.assertIn(str(MARKER_DIR / self.sid), self.prompt())


if __name__ == "__main__":
    unittest.main(verbosity=2)
