#!/usr/bin/env python3
"""Tests for session_reduce — the shared session-transcript reduction module.

Pins the pipeline (A1-A4 structural cleanup, active-path reconstruction,
S1-S6 staged reduction, render) that third-party-review and catch-up used to
carry as diverging verbatim copies with zero coverage on either side.
"""
import unittest

import session_reduce as sr


def msg(role, content):
    return {"role": role, "content": content}


def make_raw():
    """A tiny session tree: 2 human turns, tool traffic, a rewound branch,
    sidechain/meta noise, and a last-prompt leaf pointer."""
    read_body = "\n".join(f"src line {i}" for i in range(1, 21))
    bash_body = "\n".join(f"out {i}" for i in range(1, 51))
    return [
        {"type": "file-history-snapshot"},
        {"uuid": "u1", "parentUuid": None,
         "message": msg("user", "<system-reminder>x</system-reminder>Fix it")},
        {"uuid": "u2", "parentUuid": "u1",
         "message": msg("assistant", [
             {"type": "thinking", "thinking": "hmm " * 50},
             {"type": "tool_use", "id": "t1", "name": "Read",
              "input": {"file_path": "/p/x.py"}}])},
        {"uuid": "u3", "parentUuid": "u2",
         "message": msg("user", [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": [{"type": "text", "text": read_body}]}])},
        {"uuid": "u4", "parentUuid": "u3",
         "message": msg("assistant", [
             {"type": "tool_use", "id": "t2", "name": "Bash",
              "input": {"command": "make"}}])},
        {"uuid": "u5", "parentUuid": "u4",
         "message": msg("user", [
             {"type": "tool_result", "tool_use_id": "t2", "is_error": False,
              "content": [{"type": "text", "text": bash_body}]}])},
        {"uuid": "r1", "parentUuid": "u2",
         "message": msg("user", "rewound ask")},          # abandoned branch
        {"uuid": "s1", "parentUuid": "u5", "isSidechain": True,
         "message": msg("assistant", [{"type": "text", "text": "sub"}])},
        {"uuid": "m1", "parentUuid": "u5", "isMeta": True,
         "message": msg("user", "meta")},
        {"uuid": "u6", "parentUuid": "u5",
         "message": msg("assistant", [{"type": "text", "text": "Done."}])},
        {"type": "last-prompt", "leafUuid": "u6"},
    ]


class TestLoadEvents(unittest.TestCase):
    def test_active_path_noise_and_rewound(self):
        events, rewound, path_ok = sr.load_events(make_raw())
        self.assertTrue(path_ok)
        self.assertEqual(rewound, 1)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds.count("human"), 1)      # reminder-only stripped
        self.assertNotIn("sub", str(events))           # sidechain dropped
        human = next(e for e in events if e["kind"] == "human")
        self.assertEqual(human["text"], "Fix it")      # system-reminder gone
        results = [e for e in events if e["kind"] == "tool_result"]
        self.assertEqual({r["name"] for r in results}, {"Read", "Bash"})

    def test_no_leaf_keeps_linear_transcript(self):
        raw = [r for r in make_raw() if r.get("type") != "last-prompt"]
        events, rewound, path_ok = sr.load_events(raw)
        self.assertFalse(path_ok)
        self.assertEqual(rewound, 0)
        # linear fallback keeps the rewound branch's human turn too
        self.assertEqual(
            sum(1 for e in events if e["kind"] == "human"), 2)


class TestClipAndStages(unittest.TestCase):
    def test_clip_lines_elides_middle_and_caps_chars(self):
        text = "\n".join(f"l{i}" for i in range(100))
        clipped = sr.clip_lines(text, 5, 2, 10_000)
        self.assertIn("[93 lines elided]", clipped)
        self.assertTrue(clipped.startswith("l0"))
        self.assertTrue(clipped.endswith("l99"))

    def test_apply_A4_truncates_read_results(self):
        events, _, _ = sr.load_events(make_raw())
        sr.apply_A4(events)
        read = next(e for e in events
                    if e["kind"] == "tool_result" and e["name"] == "Read")
        self.assertIn("[Read: /p/x.py, 20 lines total]", read["text"])
        self.assertEqual(
            len([ln for ln in read["text"].splitlines()]) - 1,
            sr.READ_HEAD_LINES)

    def test_reduce_events_under_budget_applies_no_stages(self):
        events, _, _ = sr.load_events(make_raw())
        original, reduced, applied = sr.reduce_events(events)
        self.assertEqual(applied, [])
        self.assertEqual(original, reduced)

    def test_reduce_events_over_budget_applies_stages_in_order(self):
        events, _, _ = sr.load_events(make_raw())
        original, reduced, applied = sr.reduce_events(events, target_tokens=1)
        self.assertEqual(applied, ["S1", "S2", "S3", "S4", "S5", "S6"])
        self.assertLess(reduced, original)
        thinking = next(e for e in events if e["kind"] == "thinking")
        self.assertRegex(thinking["text"], r"\[thinking: \d+ chars elided\]")


class TestRender(unittest.TestCase):
    def test_render_shape(self):
        events, _, _ = sr.load_events(make_raw())
        out = sr.render(events, {"h": 1})
        self.assertTrue(out.startswith("```json"))
        self.assertIn("## 🙋 Human — turn 1", out)
        self.assertIn("### 🔧 Read", out)
        self.assertIn("### ↳ Bash result", out)


class TestConfig(unittest.TestCase):
    def test_monotonicity_invariant_holds(self):
        # S4 must cut at least as hard as S1, or S4 is a dead stage.
        self.assertLessEqual(sr.S4_HEAD_LINES, sr.S1_HEAD_LINES)
        self.assertLessEqual(sr.S4_MAX_CHARS, sr.S1_MAX_CHARS)

    def test_validate_rejects_a_broken_knob(self):
        with self.assertRaises(ValueError):
            sr._validate({"TARGET_TOKENS": -1})


if __name__ == "__main__":
    unittest.main()
