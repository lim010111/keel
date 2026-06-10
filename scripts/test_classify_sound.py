#!/usr/bin/env python3
"""Gold-set tests for classify_sound. Tails are real (last ~80 chars) final-turn
texts harvested from grill/work session transcripts during the investigation.
Run: python3 scripts/test_classify_sound.py"""
import json
import os
import tempfile
import unittest

from classify_sound import classify, extract_tail, resolve_tail


def _transcript(entries):
    """entries: list of (stop_reason, text) or (stop_reason, text, msg_id). stop_reason
    '_user' writes an interleaved non-assistant line (tool_result noise) instead.
    extract_tail keys on the assistant stop_reason AND message.id, so fixtures can vary
    both. Returns a temp file path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for entry in entries:
            sr, text = entry[0], entry[1]
            mid = entry[2] if len(entry) > 2 else None
            if sr == "_user":
                fh.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
                continue
            content = [{"type": "text", "text": text}] if text else []
            msg = {"stop_reason": sr, "content": content}
            if mid is not None:
                msg["id"] = mid
            fh.write(json.dumps({"type": "assistant", "message": msg}) + "\n")
    return path


class TestClassify(unittest.TestCase):
    # --- clear QUESTION turns: must play the question sound ---
    QUESTIONS = [
        '이 2-레이어 + "지원이 원자 단위" 모델에 동의하나요? 아니면 당신 머릿속의 중심 축은 다른가요?',
        '구성에 동의하나요? **빠진 것**이나 **겹쳐서 합쳐야 할 것**이 보이나요?',
        '이 트리 골격 + 상단 설계 포인트에 동의하나요? 그리고 포크 (가)(나)는 어떻게?',
        '필요하면 재편 변경을 커밋해 안전망을 한 단계 더 굳힐 수 있습니다 — 원하시면 말씀해주세요.',
        '어느 쪽인지, 혹은 다른 지점인지 말씀해 주세요.',
        '어느 것부터 갈까? (1번은 한 줄이면 정리되니 같이 답해줘.)',
        'ADR-0009 최종 검토 부탁해 — 더 잡을 곳 있으면 말해주고, 없으면 land 승인해줘.',
        '더 잡을 곳 있으면 말해줘. 없으면 승인 신호만 주면 1→2→3 진행할게.',
        'ADR-0009 land 승인 + supersede-note 방식(노트만 vs 표수정)만 정해주면 1~5 진행할게.',
        '동의하시면 다음 grilling 질문 없이 바로 구현 진입. 다른 우려 있으면 그것부터 풀고 갑시다.',
        '지금 검증하실래요, 아니면 다음 세션으로 넘길까요?',
        '② design 문서는 my_blog로 이관할까요, my_portfolio에 design-intent로 남길까요?',
    ]

    # The skill ask-marker is decisive even mid-text.
    MARKER_QUESTION = (
        '계획은 위와 같습니다.\n**묻고 싶은 것:** 이 4요소 해부도 + 별도 Status 퍼널에 동의하나요?'
    )

    # Two-line window: the "?" sits on the second-to-last line (AUTO32 in the data).
    TWO_LINE_QUESTION = (
        '먼저 화면에 두 줄이 나오나요?\n둘 다 ✓이면 A — 그대로 영상 촬영 가도 됩니다.'
    )

    # --- genuinely ambiguous tails: deterministic pass must defer (not guess) ---
    # These are the 알려줘/report-back crux; the LLM fallback resolves them.
    AMBIGUOUS = [
        '촬영 결과 잘 나오면 알려주세요. SDK scope cleanup은 verification 통과 후 후속 작업으로.',
        '업로드 끝나면 URL 알려주시면 description 최종 확인 가능.',
        '(영상은 YouTube에 올라가 있으니 ...) 검수 결과 받으면 알려주세요.',
        'modal에 표시되는 Web app URL이 다르지 않은지 확인해주세요. 다르면 즉시 중단하고 알려주세요.',
        '...스크린샷 보내주시면 방법 B의 정확한 URL/Visibility 설정 짚어드릴 수 있어요.',
    ]

    # --- clear COMPLETION turns: must play the complete sound ---
    COMPLETIONS = [
        '이미 master-resume.md에 다 있어서, 다음 세션에서 transcribe만 하면 됩니다 (01 이슈 → ready-for-agent).',
        'reframe → local profile 구현 issue 1~5 진행할게.',
        'Done. Updated the parser and re-ran the suite — all 42 tests pass.',
        'Refactored the merge-gate scheduler and removed the orphaned import. No behavior change.',
        '커밋했습니다. STATUS.md 진행률도 갱신됐어요.',
    ]

    def test_questions(self):
        for t in self.QUESTIONS:
            self.assertEqual(classify(t)[0], "question", msg=t)

    def test_marker(self):
        self.assertEqual(classify(self.MARKER_QUESTION), ("question", "marker"))

    def test_two_line_window(self):
        self.assertEqual(classify(self.TWO_LINE_QUESTION)[0], "question")

    def test_ambiguous_defers(self):
        for t in self.AMBIGUOUS:
            self.assertEqual(classify(t)[0], "ambiguous", msg=t)

    def test_completions(self):
        for t in self.COMPLETIONS:
            self.assertEqual(classify(t)[0], "complete", msg=t)

    def test_empty(self):
        self.assertEqual(classify("")[0], "complete")
        self.assertEqual(classify("   \n  ")[0], "complete")

    def test_window_ignores_early_question(self):
        # A completion that merely *mentions* a question earlier must not fire.
        tail = (
            "아까 물어본 스키마 질문은 해결됐나요? 라고 했었죠.\n"
            "어쨌든 7칸 스키마로 확정하고 파일에 반영했습니다.\n"
            "테스트도 통과합니다."
        )
        self.assertEqual(classify(tail)[0], "complete")


class TestExtractTail(unittest.TestCase):
    """The latest turn must end cleanly with end_turn to be classified. tool_use-final
    turns (AskUserQuestion waits — owned by PermissionRequest — and interrupts) must
    yield "" so they default to 'complete', never reading a stale earlier turn."""

    def _run(self, entries):
        p = _transcript(entries)
        try:
            return extract_tail(p)
        finally:
            os.unlink(p)

    def test_clean_end_turn(self):
        self.assertEqual(self._run([("end_turn", "다음으로 갈까요?")]), "다음으로 갈까요?")

    def test_tool_use_final_interrupt_yields_empty(self):
        # real shape: agent ran a Bash tool then Stop fired mid-turn (no final end_turn)
        out = self._run([
            ("end_turn", "이전 턴 질문인가요?"),         # stale earlier turn
            ("_user", "answer"),
            ("tool_use", "전체 검토 완료. 9/10."),        # latest = tool_use
            ("tool_use", ""),
        ])
        self.assertEqual(out, "")

    def test_askuserquestion_final_yields_empty(self):
        # AskUserQuestion is a tool_use; it never fires Stop, but guard it anyway.
        out = self._run([
            ("end_turn", "앞 턴 완료했습니다."),
            ("_user", "ok"),
            ("tool_use", ""),                            # the AskUserQuestion call
        ])
        self.assertEqual(out, "")

    def test_multi_entry_end_turn_run_concatenates(self):
        # one visual turn = a thinking-only end_turn then a text end_turn
        out = self._run([
            ("tool_use", ""),
            ("end_turn", ""),                            # thinking-only
            ("end_turn", "이 설계에 동의하나요?"),
        ])
        self.assertEqual(out, "이 설계에 동의하나요?")

    def test_tool_use_in_turn_then_clean_end_turn(self):
        # tool calls earlier in the turn don't matter; the final end_turn text wins
        out = self._run([
            ("tool_use", ""),
            ("_user", "tool result"),
            ("end_turn", "테스트 통과했습니다."),
        ])
        self.assertEqual(out, "테스트 통과했습니다.")

    def test_missing_file(self):
        self.assertEqual(extract_tail("/no/such/transcript.jsonl"), "")

    def test_prior_turn_text_not_leaked_across_id_boundary(self):
        # Regression (workflow MAJOR): _read_entries drops the user line, so a prior
        # turn's end_turn+text (msgA) sits adjacent to THIS turn's only-flushed
        # thinking chunk (end_turn, empty, msgB). The trailing run must stop at the
        # msg.id boundary and yield "" (→ poll/complete), NOT the prior turn's text.
        out = self._run([
            ("end_turn", "커밋 완료했습니다. 테스트도 통과합니다.", "msgA"),
            ("_user", "다음 질문"),
            ("end_turn", "", "msgB"),       # this turn so far: thinking chunk only
        ])
        self.assertEqual(out, "")

    def test_same_id_chunks_still_concatenate(self):
        # Within ONE message.id a thinking chunk + text chunk still merge.
        out = self._run([
            ("end_turn", "", "msgB"),
            ("end_turn", "이 설계에 동의하나요?", "msgB"),
        ])
        self.assertEqual(out, "이 설계에 동의하나요?")


class TestResolveTail(unittest.TestCase):
    """When the Stop hook fires, this turn's end_turn chunks may not have flushed,
    so the file can still end at a thinking chunk (end_turn, no text) OR at a stale
    prior tool_use (turn continued from an AskUserQuestion). resolve_tail polls on
    ANY empty tail until the text lands; only a genuine interrupt exhausts."""

    @staticmethod
    def _reader(seq, calls):
        def read():
            i = min(calls["n"], len(seq) - 1)
            calls["n"] += 1
            return seq[i]
        return read

    # Entries are (stop_reason, text, msg_id); chunks of one turn share an id.
    def test_retry_waits_for_text_chunk(self):
        calls = {"n": 0}
        seq = [
            [("end_turn", "", "m1")],                                   # thinking only
            [("end_turn", "", "m1"), ("end_turn", "동의하나요?", "m1")],  # text landed
        ]
        tail = resolve_tail(self._reader(seq, calls), lambda s: None,
                            retries=5, delay=0)
        self.assertEqual(tail, "동의하나요?")
        self.assertGreaterEqual(calls["n"], 2)  # it re-read at least once

    def test_retries_through_stale_tool_use(self):
        # The regression: Stop fired before a post-AskUserQuestion turn's chunks
        # flushed, so the file still ended at the prior AUQ tool_use. We must poll
        # through it and classify the question text once it lands, not bail early.
        calls = {"n": 0}
        seq = [
            [("tool_use", "", "m0")],                                          # stale AUQ
            [("tool_use", "", "m0"), ("end_turn", "", "m1")],                  # thinking
            [("tool_use", "", "m0"), ("end_turn", "", "m1"),
             ("end_turn", "들리셨나요?", "m1")],                                # text
        ]
        tail = resolve_tail(self._reader(seq, calls), lambda s: None,
                            retries=10, delay=0)
        self.assertEqual(tail, "들리셨나요?")

    def test_genuine_interrupt_exhausts_to_empty(self):
        # A real interrupt truly ends at tool_use and never gains text -> complete.
        calls = {"n": 0}
        seq = [[("tool_use", "전체 검토 완료. 9/10.", "m0")]]  # stable tool_use-final
        tail = resolve_tail(self._reader(seq, calls), lambda s: None,
                            retries=3, delay=0)
        self.assertEqual(tail, "")
        self.assertEqual(calls["n"], 4)  # initial read + 3 retries, then give up

    def test_gives_up_on_persistent_empty(self):
        calls = {"n": 0}
        seq = [[("end_turn", "", "m1")]]  # text never lands
        tail = resolve_tail(self._reader(seq, calls), lambda s: None,
                            retries=3, delay=0)
        self.assertEqual(tail, "")
        self.assertEqual(calls["n"], 4)  # initial read + 3 retries

    def test_clean_text_no_retry(self):
        calls = {"n": 0}
        seq = [[("end_turn", "테스트 통과했습니다.", "m1")]]
        tail = resolve_tail(self._reader(seq, calls), lambda s: None,
                            retries=5, delay=0)
        self.assertEqual(tail, "테스트 통과했습니다.")
        self.assertEqual(calls["n"], 1)


class TestPrimarySource(unittest.TestCase):
    """main() classifies payload['last_assistant_message'] directly (deterministic,
    race-free), and falls back to the transcript only when the field is absent."""

    def _decision(self, payload):
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(
            ["python3", os.path.join(here, "classify_sound.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "SOUND_DIAG": "0", "SOUND_LLM_FALLBACK": "0"},
        )
        return out.stdout.strip().split("\t")[0]

    def test_payload_question_needs_no_transcript(self):
        # A question in last_assistant_message classifies as question even though the
        # transcript_path does not exist -> proves the payload field is the source.
        self.assertEqual(self._decision({
            "last_assistant_message": "이 설계에 동의하나요?",
            "transcript_path": "/no/such/file.jsonl",
        }), "question")

    def test_payload_completion(self):
        self.assertEqual(self._decision({
            "last_assistant_message": "커밋했습니다. 테스트 통과합니다.",
            "transcript_path": "/no/such/file.jsonl",
        }), "complete")

    def test_absent_field_falls_back_then_completes(self):
        # No last_assistant_message + missing transcript -> graceful complete.
        self.assertEqual(self._decision({
            "transcript_path": "/no/such/file.jsonl",
        }), "complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
