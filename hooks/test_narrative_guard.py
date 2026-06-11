#!/usr/bin/env python3
"""Tests for narrative_guard.py (claude-harness-work status-harness#01).

Stdlib unittest only — pytest is not installed in this environment.
Run:  python3 hooks/test_narrative_guard.py -v

Isolation: every subprocess invocation points the hook's state dir at a fresh
temp dir via NARRATIVE_GUARD_STATE_ROOT, so the real
~/.claude/hooks/.narrative-guard-state/ is never touched. setUpModule /
tearDownModule assert that invariant as defence-in-depth (mirror
test_tdd_hooks.py). Each test uses a synthetic VERIFY-<uuid> session id.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
SCRIPTS = HOOKS.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HOOKS))

import status  # noqa: E402

GUARD = HOOKS / "narrative_guard.py"
REAL_STATE = Path.home() / ".claude" / "hooks" / ".narrative-guard-state"


# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------
def git_init(path: Path) -> dict:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    return env


def seed_issue(root: Path, feature: str, nn: str, slug: str, *, total: int = 2,
               done: int = 0, status_line: str = "ready-for-agent",
               blockers=None, resolution: bool = False, title: str | None = None) -> Path:
    d = root / ".scratch" / feature / "issues"
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title or slug.replace('-', ' ').title()}", "",
             f"Status: {status_line}", "", "## Acceptance criteria", ""]
    for i in range(total):
        box = "x" if i < done else " "
        lines.append(f"- [{box}] criterion {i + 1}")
    lines += ["", "## Blocked by", ""]
    if blockers:
        for b in blockers:
            lines.append(f"- #{b}")
    else:
        lines.append("None — can start immediately.")
    if resolution:
        lines += ["", "> **Resolution:** landed via commit abc123."]
    p = d / f"{nn}-{slug}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_status(root: Path, narrative_body: str, *, markers: bool = True) -> Path:
    if markers:
        body = (f"# Project Status\n\n{status.NARRATIVE_START}\n{narrative_body}\n"
                f"{status.NARRATIVE_END}\n\n## feat\n\n(generated table)\n")
    else:
        body = f"# Project Status\n\n{narrative_body}\n\n## feat\n\n(generated table)\n"
    p = root / "STATUS.md"
    p.write_text(body, encoding="utf-8")
    return p


def run_guard(subcmd: str, payload: dict, *, state_root, env_extra=None):
    env = {**os.environ, "NARRATIVE_GUARD_STATE_ROOT": str(state_root)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["python3", str(GUARD), subcmd],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=60,
    )


def basic_repo(td: str, **issue_kw) -> Path:
    """A git repo with one feature/issue and a STATUS.md that has markers."""
    root = Path(td)
    git_init(root)
    seed_issue(root, "feat", "01", "alpha", **issue_kw)
    write_status(root, "## Current focus\n\nDoing alpha.\n")
    return root


def vid() -> str:
    return f"VERIFY-{uuid.uuid4()}"


def snap_files(sr):
    """All snapshot files under a state root, regardless of the key encoding
    (snapshots are keyed by session AND repo root, so locate them by glob)."""
    return sorted(Path(sr).glob("snapshot-*.json"))


def the_snap(sr):
    fs = snap_files(sr)
    assert len(fs) == 1, f"expected exactly one snapshot, got {[f.name for f in fs]}"
    return fs[0]


# --------------------------------------------------------------------------
# isolation guard (defence-in-depth; tests use NARRATIVE_GUARD_STATE_ROOT)
# --------------------------------------------------------------------------
_SNAPSHOT: dict = {}


def _names(d: Path):
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def setUpModule():
    _SNAPSHOT["real"] = _names(REAL_STATE)


def tearDownModule():
    after = _names(REAL_STATE)
    assert after == _SNAPSHOT["real"], (
        "real .narrative-guard-state changed — a test leaked out of its "
        "NARRATIVE_GUARD_STATE_ROOT sandbox!", _SNAPSHOT["real"], after)


# --------------------------------------------------------------------------
# slice 1 — snapshot writes a session-keyed snapshot file (A1)
# --------------------------------------------------------------------------
class TestSnapshot(unittest.TestCase):
    def test_snapshot_writes_keyed_file(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            r = run_guard("snapshot", {"session_id": sid, "cwd": str(root)}, state_root=sr)
            self.assertEqual(r.returncode, 0, r.stderr)
            snaps = snap_files(sr)
            self.assertEqual(len(snaps), 1,
                             f"expected one snapshot; dir={_names(Path(sr))} stderr={r.stderr}")
            data = json.loads(snaps[0].read_text())
            self.assertIn("fingerprint", data)
            self.assertIn("narrative_hash", data)
            self.assertIsInstance(data["fingerprint"], dict)


# --------------------------------------------------------------------------
# slice 2 — has_resolution: column-0 anchor, not substring (A2; footgun #2)
# --------------------------------------------------------------------------
class TestResolutionAnchor(unittest.TestCase):
    def test_anchor_rejects_prose_accepts_real_block(self):
        import narrative_guard as ng
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Prose mentions of the literal — like this very issue's own body:
            # a table cell and an indented bullet. Must NOT count as present.
            prose = d / "prose.md"
            prose.write_text(
                "# X\n\nStatus: ready-for-agent\n\n## Acceptance criteria\n\n"
                "- [ ] per-issue `> **Resolution:**` block presence\n\n"
                "| Posture | + per-issue `> **Resolution:**` block presence |\n",
                encoding="utf-8")
            # A real, column-0 Resolution block. Must count as present.
            real = d / "real.md"
            real.write_text(
                "# Y\n\nStatus: done\n\n## Acceptance criteria\n\n- [x] a\n\n"
                "> **Resolution:** shipped in claude-config commit abc123.\n",
                encoding="utf-8")
            self.assertFalse(ng.has_resolution(prose))
            self.assertTrue(ng.has_resolution(real))


# --------------------------------------------------------------------------
# slice 3 — fingerprint reflects all 4 posture dimensions (A2, A8)
# --------------------------------------------------------------------------
class TestFingerprintDimensions(unittest.TestCase):
    def _fp(self, root):
        import narrative_guard as ng
        return json.dumps(ng.compute_fingerprint(root), sort_keys=True)

    def test_each_dimension_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)
            base = self._fp(root)

            # (1) lifecycle state: flip checkboxes to done -> state todo->done.
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)
            self.assertNotEqual(base, self._fp(root), "lifecycle dim not in fp")
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)
            self.assertEqual(base, self._fp(root), "fingerprint not deterministic")

            # (2) Resolution-block presence.
            seed_issue(root, "feat", "01", "alpha", total=2, done=0, resolution=True)
            self.assertNotEqual(base, self._fp(root), "resolution dim not in fp")
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)

            # (3) issue / feature set: add a new issue.
            beta = seed_issue(root, "feat", "02", "beta", total=1, done=0)
            self.assertNotEqual(base, self._fp(root), "issue-set dim not in fp")
            beta.unlink()
            self.assertEqual(base, self._fp(root))

            # (4) docs/adr/ fingerprint.
            adr = root / "docs" / "adr"
            adr.mkdir(parents=True)
            (adr / "0001-x.md").write_text("# adr one\n", encoding="utf-8")
            self.assertNotEqual(base, self._fp(root), "adr dim not in fp")


# --------------------------------------------------------------------------
# slice 4 — check blocks: posture changed + narrative unchanged (A3)
# --------------------------------------------------------------------------
def _snapshot(root, sid, sr):
    return run_guard("snapshot", {"session_id": sid, "cwd": str(root)}, state_root=sr)


def _check(root, sid, sr, **payload):
    return run_guard("check", {"session_id": sid, "cwd": str(root), **payload},
                     state_root=sr)


class TestCheckBlocks(unittest.TestCase):
    def test_block_when_posture_changed_narrative_unchanged(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            self.assertEqual(_snapshot(root, sid, sr).returncode, 0)
            snap = the_snap(sr)
            before = snap.read_text()
            # Posture change (boxes -> done); narrative (STATUS.md) untouched.
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2, f"expected block; stderr={r.stderr}")
            self.assertTrue(r.stderr.strip(), "block must explain itself on stderr")
            self.assertEqual(snap.read_text(), before,
                             "block path must NOT re-baseline the snapshot")


# --------------------------------------------------------------------------
# slice 5 — check allows: narrative also changed (re-baseline) / nothing changed
# --------------------------------------------------------------------------
class TestCheckAllows(unittest.TestCase):
    def test_no_block_when_narrative_also_changed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            snap = the_snap(sr)
            before = snap.read_text()
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)     # posture change
            write_status(root, "## Current focus\n\nAlpha is done now.\n")  # narrative edit
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 0, f"agent updated narrative; stderr={r.stderr}")
            self.assertNotEqual(snap.read_text(), before,
                                "narrative change should re-baseline the snapshot")

    def test_no_block_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 0, r.stderr)


# --------------------------------------------------------------------------
# slice 6 — P1: a mid-issue AC count bump must NOT fire (A3 / principle P1)
# --------------------------------------------------------------------------
class TestP1CountBump(unittest.TestCase):
    def test_midissue_count_bump_does_not_fire(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=3, done=1)  # in-progress
            write_status(root, "## Current focus\n\nGrinding alpha.\n")
            sid = vid()
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=3, done=2)  # still in-progress
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 0,
                             f"an AC count bump (1/3->2/3) must not block; stderr={r.stderr}")


# --------------------------------------------------------------------------
# slice 7 — re-baseline re-arms the guard for a 2nd same-session change (A5)
# --------------------------------------------------------------------------
class TestReBaseline(unittest.TestCase):
    def test_rebaseline_catches_second_change(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            # Change #1 + narrative edit -> re-baseline + allow.
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)
            write_status(root, "## Current focus\n\nAlpha is done.\n")
            self.assertEqual(_check(root, sid, sr).returncode, 0)
            # Change #2 (new ADR), narrative untouched since the re-baseline -> block.
            adr = root / "docs" / "adr"
            adr.mkdir(parents=True)
            (adr / "0001-x.md").write_text("# adr one\n", encoding="utf-8")
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2,
                             f"2nd posture change must block after re-baseline; stderr={r.stderr}")


# --------------------------------------------------------------------------
# slice 8 — stop_hook_active loop guard (A4)
# --------------------------------------------------------------------------
class TestLoopGuard(unittest.TestCase):
    def test_stop_hook_active_short_circuits(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # block-eligible
            r = _check(root, sid, sr, stop_hook_active=True)
            self.assertEqual(r.returncode, 0,
                             f"loop guard must allow once-per-turn; stderr={r.stderr}")


# --------------------------------------------------------------------------
# slices 9-12 — fail-open / opt-in guards (A4, A7)
# --------------------------------------------------------------------------
class TestFailOpen(unittest.TestCase):
    def test_missing_snapshot_allows(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=2)  # block-eligible, but no snapshot taken
            r = _check(root, vid(), sr)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_non_issue_project_allows_and_snapshots_nothing(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = Path(td)
            git_init(root)  # a git repo with NO .scratch/*/issues/*.md
            write_status(root, "## Current focus\n\nx\n")
            sid = vid()
            self.assertEqual(_snapshot(root, sid, sr).returncode, 0)
            self.assertEqual(snap_files(sr), [],
                             "non-issue project must not be snapshotted")
            self.assertEqual(_check(root, sid, sr).returncode, 0)

    def test_corrupt_snapshot_allows(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            the_snap(sr).write_text("{not json")  # corrupt
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # would block
            self.assertEqual(_check(root, sid, sr).returncode, 0)

    def test_status_without_markers_noops(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)
            write_status(root, "Current focus: x", markers=False)  # no narrative markers
            sid = vid()
            self.assertEqual(_snapshot(root, sid, sr).returncode, 0)
            self.assertEqual(snap_files(sr), [],
                             "no markers -> opt-out, no snapshot")
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # posture change
            self.assertEqual(_check(root, sid, sr).returncode, 0)


# --------------------------------------------------------------------------
# slice 13 — kill-switch + merge-gate producer guard (A7; preflight risk #..)
# --------------------------------------------------------------------------
class TestDisableSwitches(unittest.TestCase):
    def test_disabled_env_does_not_block(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # block-eligible
            r = run_guard("check", {"session_id": sid, "cwd": str(root)}, state_root=sr,
                          env_extra={"NARRATIVE_GUARD_DISABLED": "1"})
            self.assertEqual(r.returncode, 0, f"kill-switch must disable block; stderr={r.stderr}")

    def test_disabled_env_snapshots_nothing(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            r = run_guard("snapshot", {"session_id": sid, "cwd": str(root)}, state_root=sr,
                          env_extra={"NARRATIVE_GUARD_DISABLED": "1"})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(snap_files(sr), [])

    def test_merge_gate_producer_skips_both_modes(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            r0 = run_guard("snapshot", {"session_id": sid, "cwd": str(root)}, state_root=sr,
                           env_extra={"MERGE_GATE_PRODUCER_RUNNING": "1"})
            self.assertEqual(r0.returncode, 0)
            self.assertEqual(snap_files(sr), [],
                             "producer subprocess must not write junk snapshots")
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # block-eligible
            r1 = run_guard("check", {"session_id": sid, "cwd": str(root)}, state_root=sr,
                           env_extra={"MERGE_GATE_PRODUCER_RUNNING": "1"})
            self.assertEqual(r1.returncode, 0, f"producer subprocess must not block; stderr={r1.stderr}")


# --------------------------------------------------------------------------
# fix G — grilling pause: pause/resume suppress the per-turn block (FP fix)
#
# A grilling skill (grill-with-docs / harden-issue) edits ADRs / CONTEXT / issue
# files inline as decisions crystallise — genuine posture movement — but defers
# the narrative refresh to the end of grilling. Without a pause, `check` blocks
# at the end of every grilling turn that touched an ADR (a false positive that
# pollutes the session). `pause` reads the session id from CLAUDE_CODE_SESSION_ID
# (== the Stop hook's stdin session_id) and the repo from cwd, so the marker is
# keyed the same (session, repo) way as the snapshot.
# --------------------------------------------------------------------------
def pause_files(sr):
    return sorted(Path(sr).glob("pause-*.json"))


def run_pause(subcmd, root, sid, sr):
    """Invoke pause/resume the way the agent's Bash tool would: session id via
    CLAUDE_CODE_SESSION_ID env, repo via cwd. No stdin payload."""
    env = {**os.environ, "NARRATIVE_GUARD_STATE_ROOT": str(sr),
           "CLAUDE_CODE_SESSION_ID": sid}
    return subprocess.run(
        ["python3", str(GUARD), subcmd], cwd=str(root),
        capture_output=True, text=True, env=env, timeout=60)


class TestGrillingPause(unittest.TestCase):
    def test_pause_suppresses_otherwise_blocking_change(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            # An ADR appears mid-grilling — the exact thing that was a false
            # positive. With a pause in place, check must NOT block.
            self.assertEqual(run_pause("pause", root, sid, sr).returncode, 0)
            self.assertEqual(len(pause_files(sr)), 1,
                             "pause must write one (session, repo)-keyed marker")
            adr = root / "docs" / "adr"
            adr.mkdir(parents=True)
            (adr / "0001-x.md").write_text("# adr one\n", encoding="utf-8")
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 0,
                             f"pause must suppress the mid-grilling block; stderr={r.stderr}")

    def test_resume_rearms_the_guard(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            run_pause("pause", root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # posture change
            self.assertEqual(_check(root, sid, sr).returncode, 0, "paused -> no block")
            # End of grilling: resume re-arms; the deferred posture change now
            # legitimately blocks until the narrative is refreshed.
            self.assertEqual(run_pause("resume", root, sid, sr).returncode, 0)
            self.assertEqual(pause_files(sr), [], "resume must remove the marker")
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2,
                             f"after resume the real posture change must block; stderr={r.stderr}")

    def test_pause_is_session_repo_scoped(self):
        with tempfile.TemporaryDirectory() as p, tempfile.TemporaryDirectory() as q, \
                tempfile.TemporaryDirectory() as sr:
            rootP = basic_repo(p, total=2, done=0)
            rootQ = basic_repo(q, total=2, done=0)
            sid = vid()
            _snapshot(rootP, sid, sr)
            _snapshot(rootQ, sid, sr)
            run_pause("pause", rootP, sid, sr)  # pause ONLY repo P
            seed_issue(rootQ, "feat", "01", "alpha", total=2, done=2)  # change in Q
            r = _check(rootQ, sid, sr)
            self.assertEqual(r.returncode, 2,
                             f"a pause in repo P must not silence the guard in repo Q; stderr={r.stderr}")

    def test_resume_without_marker_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            r = run_pause("resume", root, vid(), sr)
            self.assertEqual(r.returncode, 0, f"resume with no marker must be a clean no-op; stderr={r.stderr}")


# --------------------------------------------------------------------------
# status-harness#03 — completion-label lint: you clean what you wrote
#
# A finished track's line is deleted, never relabelled `- **완료**`. The guard
# blocks only when the narrative changed this session AND carries completion
# labels; legacy offenders with an untouched narrative are the advisory
# banner's job (status.py), not a block for someone else's mess.
# --------------------------------------------------------------------------
class TestCompletionLint(unittest.TestCase):
    DIRTY = "## Current focus\n\nx\n\n- **완료 · feat #01:** alpha shipped.\n"

    def test_blocks_when_written_this_session(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            snap = the_snap(sr)
            before = snap.read_text()
            write_status(root, self.DIRTY)  # narrative edit introduces the label
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2, f"completion label must block; stderr={r.stderr}")
            self.assertIn("완료 · feat #01", r.stderr, "block must quote the offending line")
            self.assertIn("Resolution", r.stderr, "block must say where the content goes")
            self.assertEqual(snap.read_text(), before,
                             "lint block must NOT re-baseline the snapshot")

    def test_clears_once_lines_removed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            write_status(root, self.DIRTY)
            self.assertEqual(_check(root, sid, sr).returncode, 2)
            write_status(root, "## Current focus\n\nAlpha shipped; next beta.\n")
            self.assertEqual(_check(root, sid, sr).returncode, 0,
                             "removing the lines must clear the block")

    def test_legacy_offender_with_untouched_narrative_does_not_block(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)
            write_status(root, self.DIRTY)  # offender pre-dates the session
            sid = vid()
            _snapshot(root, sid, sr)
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 0,
                             f"a pre-existing offender must not block; stderr={r.stderr}")

    def test_pause_suppresses_the_lint(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            run_pause("pause", root, sid, sr)
            write_status(root, self.DIRTY)
            self.assertEqual(_check(root, sid, sr).returncode, 0,
                             "a grilling pause must suppress the lint too")


# --------------------------------------------------------------------------
# slice 14 — block message names the change and directs to /status (A6)
# --------------------------------------------------------------------------
class TestBlockMessage(unittest.TestCase):
    def test_names_lifecycle_change_and_says_status(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # todo -> done
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2)
            self.assertIn("/status", r.stderr)
            self.assertNotIn("edit the narrative", r.stderr.lower())
            self.assertIn("feat/01", r.stderr, f"must name the issue: {r.stderr}")

    def test_names_adr_change(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            adr = root / "docs" / "adr"
            adr.mkdir(parents=True)
            (adr / "0099-zeta.md").write_text("# zeta\n", encoding="utf-8")
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2)
            self.assertIn("ADR", r.stderr, f"must name the ADR change: {r.stderr}")


# --------------------------------------------------------------------------
# slice 15 — status.py stays a pure exit-0 regenerator; guard reuses it (A8)
# --------------------------------------------------------------------------
class TestStatusPyPurity(unittest.TestCase):
    def test_status_py_never_exits_2(self):
        src = (SCRIPTS / "status.py").read_text(encoding="utf-8")
        self.assertNotIn("exit(2)", src)
        self.assertNotIn("exit(1)", src)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # block-eligible
            r = subprocess.run(["python3", str(SCRIPTS / "status.py")], cwd=str(root),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_guard_imports_status_not_duplicates(self):
        gsrc = GUARD.read_text(encoding="utf-8")
        self.assertIn("import status", gsrc)
        self.assertNotIn("def parse_issue", gsrc)
        self.assertNotIn("def lifecycle", gsrc)
        self.assertNotIn("def read_narrative", gsrc)


# --------------------------------------------------------------------------
# slice 16 — snapshot is write-once per session (resume/compact safety, A1)
# --------------------------------------------------------------------------
class TestWriteOnce(unittest.TestCase):
    def test_resume_snapshot_does_not_rearm_after_change(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            sid = vid()
            _snapshot(root, sid, sr)  # anchor {fp0, nh0}
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # posture change, no narrative edit
            _snapshot(root, sid, sr)  # resume/compact re-fires SessionStart -> must be a no-op
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2,
                             "a 2nd snapshot must not re-anchor an unacknowledged change")


# --------------------------------------------------------------------------
# slice 17 — known gaps documented in-code (A9)
# --------------------------------------------------------------------------
class TestKnownGaps(unittest.TestCase):
    def test_known_gaps_documented_in_source(self):
        low = GUARD.read_text(encoding="utf-8").lower()
        self.assertIn("known gap", low)
        self.assertIn("claude code", low, "Claude-Code-only scope gap must be documented")
        self.assertIn("pure", low)
        self.assertIn("body", low)  # the uncovered pure-body-change case


# --------------------------------------------------------------------------
# slice 19 — end-to-end: flip to done -> block once -> /status clears (A10)
# --------------------------------------------------------------------------
class TestEndToEnd(unittest.TestCase):
    def test_flip_to_done_blocks_then_status_clears(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=1, done=0)
            sid = vid()
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=1, done=1)  # -> done, no narrative edit
            self.assertEqual(_check(root, sid, sr).returncode, 2, "flip-to-done must block")
            write_status(root, "## Current focus\n\nAlpha shipped; next is beta.\n")  # /status
            self.assertEqual(_check(root, sid, sr).returncode, 0, "/status must clear the block")
            self.assertEqual(_check(root, sid, sr).returncode, 0, "stays clear with no new change")


# --------------------------------------------------------------------------
# slice 20 — status.py regen preserves the narrative hash (Stop-ordering safety)
# --------------------------------------------------------------------------
class TestNarrativeHashInvariance(unittest.TestCase):
    def test_status_py_run_preserves_narrative_hash(self):
        import narrative_guard as ng
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)
            write_status(root, "## Current focus\n\nDoing alpha.\n")
            status_path = root / "STATUS.md"
            before = ng.narrative_hash(status_path)
            r = subprocess.run(["python3", str(SCRIPTS / "status.py")], cwd=str(root),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(ng.narrative_hash(status_path), before,
                             "status.py regen must leave the narrative block byte-identical")


# --------------------------------------------------------------------------
# fix A — snapshot keyed by (session, repo): cross-repo isolation (review #1/#2/#3/#5)
# --------------------------------------------------------------------------
class TestCrossRepoIsolation(unittest.TestCase):
    def test_check_in_other_repo_does_not_compare_against_first(self):
        with tempfile.TemporaryDirectory() as p, tempfile.TemporaryDirectory() as q, \
                tempfile.TemporaryDirectory() as sr:
            rootP = basic_repo(p, total=2, done=0)   # todo, narrative "Doing alpha."
            rootQ = basic_repo(q, total=2, done=2)   # done, byte-identical narrative
            sid = vid()
            _snapshot(rootP, sid, sr)  # anchor only P, same session id
            # A Stop fires with cwd=Q (worktree / second repo, same session). Q has no
            # snapshot of its own -> the guard must fail-open, NOT compare Q vs P.
            r = _check(rootQ, sid, sr)
            self.assertEqual(r.returncode, 0,
                             f"cross-repo check must not compare against another repo's "
                             f"snapshot; stderr={r.stderr}")

    def test_each_repo_anchored_independently(self):
        with tempfile.TemporaryDirectory() as p, tempfile.TemporaryDirectory() as q, \
                tempfile.TemporaryDirectory() as sr:
            rootP = basic_repo(p, total=2, done=0)
            rootQ = basic_repo(q, total=2, done=0)
            sid = vid()
            _snapshot(rootP, sid, sr)
            _snapshot(rootQ, sid, sr)
            self.assertEqual(len(snap_files(sr)), 2, "each (session, repo) gets its own anchor")
            # a real posture change in Q still blocks (its own anchor is intact)
            seed_issue(rootQ, "feat", "01", "alpha", total=2, done=2)
            self.assertEqual(_check(rootQ, sid, sr).returncode, 2)
            # ...and P is untouched by Q's activity
            self.assertEqual(_check(rootP, sid, sr).returncode, 0)


# --------------------------------------------------------------------------
# fix B — a single unreadable docs/adr/*.md must not disable the guard (review #4)
# --------------------------------------------------------------------------
class TestAdrResilience(unittest.TestCase):
    def test_broken_adr_symlink_does_not_disable_guard(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            adr = root / "docs" / "adr"
            adr.mkdir(parents=True)
            (adr / "0001-real.md").write_text("# real adr\n", encoding="utf-8")
            (adr / "0002-broken.md").symlink_to(root / "does-not-exist.md")  # matches *.md, unreadable
            sid = vid()
            _snapshot(root, sid, sr)
            self.assertEqual(len(snap_files(sr)), 1,
                             "a broken ADR symlink must not abort the snapshot")
            (adr / "0003-new.md").write_text("# new adr\n", encoding="utf-8")  # real posture change
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2,
                             "an unreadable ADR must not silently disable the guard")


# --------------------------------------------------------------------------
# fix E — duplicate issue number in one feature is tracked per-file (review #9)
# --------------------------------------------------------------------------
class TestDuplicateIssueNumber(unittest.TestCase):
    def test_dup_nn_change_is_not_shadowed(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)             # feat/01-alpha
            seed_issue(root, "feat", "01", "bravo", total=2, done=0)  # same NN, different slug
            sid = vid()
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # flip alpha only
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2,
                             "a posture change on a duplicate-NN issue must not be shadowed")


# --------------------------------------------------------------------------
# fix C — one undecodable issue file must not disable the guard (review #6)
# --------------------------------------------------------------------------
class TestIssueResilience(unittest.TestCase):
    def test_undecodable_issue_does_not_disable_guard(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0)
            bad = root / ".scratch" / "feat" / "issues" / "02-bad.md"
            bad.write_bytes(b"# bad\n\nStatus: ready\n\xff\xfe not valid utf-8\n")
            sid = vid()
            _snapshot(root, sid, sr)
            self.assertEqual(len(snap_files(sr)), 1,
                             "an undecodable issue file must not abort the snapshot")
            seed_issue(root, "feat", "01", "alpha", total=2, done=2)  # real change on the good issue
            r = _check(root, sid, sr)
            self.assertEqual(r.returncode, 2,
                             "an undecodable issue must not silently disable the guard")


# --------------------------------------------------------------------------
# fix D — has_markers agrees with read_narrative on what "has a narrative" means (review #8)
# --------------------------------------------------------------------------
class TestMarkersMatchReadNarrative(unittest.TestCase):
    def test_malformed_one_line_markers_opt_out(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = Path(td)
            git_init(root)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0)
            # Markers both present but on ONE line: read_narrative's START\\n..\\nEND
            # regex will NOT match, so it would hash DEFAULT_NARRATIVE. has_markers
            # must agree (opt out) rather than anchoring a hash the agent can't move.
            (root / "STATUS.md").write_text(
                f"# Project Status\n\n{status.NARRATIVE_START} oops {status.NARRATIVE_END}\n\n## feat\n",
                encoding="utf-8")
            sid = vid()
            _snapshot(root, sid, sr)
            self.assertEqual(snap_files(sr), [],
                             "markers read_narrative can't parse must opt out (no snapshot)")


# --------------------------------------------------------------------------
# fix F — triage transitions (parked/wontfix) are posture changes (review #7)
# --------------------------------------------------------------------------
class TestTriageTransitions(unittest.TestCase):
    def _transition_fires(self, new_status):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sr:
            root = basic_repo(td, total=2, done=0, status_line="ready-for-agent")
            sid = vid()
            _snapshot(root, sid, sr)
            seed_issue(root, "feat", "01", "alpha", total=2, done=0, status_line=new_status)
            return _check(root, sid, sr).returncode

    def test_parked_transition_fires(self):
        self.assertEqual(self._transition_fires("parked"), 2,
                         "ready-for-agent -> parked is a posture change and must fire")

    def test_wontfix_transition_fires(self):
        self.assertEqual(self._transition_fires("wontfix"), 2,
                         "ready-for-agent -> wontfix is a posture change and must fire")


# --------------------------------------------------------------------------
# slice 18 — settings.json wiring: SessionStart snapshot + Stop check (A10)
# --------------------------------------------------------------------------
class TestSettingsWiring(unittest.TestCase):
    def _commands(self, hooks, event):
        return [h.get("command", "")
                for entry in hooks.get(event, []) for h in entry.get("hooks", [])]

    def test_settings_wires_snapshot_and_check(self):
        cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        hooks = cfg["hooks"]
        ss = self._commands(hooks, "SessionStart")
        stop = self._commands(hooks, "Stop")
        self.assertTrue(any("narrative_guard.py snapshot" in c for c in ss),
                        f"SessionStart missing snapshot: {ss}")
        self.assertTrue(any("narrative_guard.py check" in c for c in stop),
                        f"Stop missing check: {stop}")
        i_status = next(i for i, c in enumerate(stop) if "status.py" in c)
        i_check = next(i for i, c in enumerate(stop) if "narrative_guard.py check" in c)
        self.assertLess(i_status, i_check,
                        "narrative_guard check must run AFTER status.py in Stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
