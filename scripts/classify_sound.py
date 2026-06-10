#!/usr/bin/env python3
"""Classify whether the last assistant turn ended with a *question to the user*
(awaiting a free-text answer) versus a *task completion*, so the Stop hook can
play the right sound. See scripts/sound_complete.sh (caller) and
scripts/test_classify_sound.py (gold-set tests).

Why this exists: a free-text question turn and a completion turn are byte-identical
at the API level — both are stop_reason=="end_turn" and both fire the Stop hook,
with no distinguishing metadata (confirmed against real transcripts + upstream
claude-code issues #13024/#12048). The only signal is the *text* of the final turn.

Strategy (the user picked "heuristic + Haiku fallback"):
  1. Fast deterministic pass over the last two lines of the final turn:
     - the "묻고 싶은 것" ask-marker, a trailing "?"/"？", or a Korean
       polite-question/decision-request ending  -> QUESTION
  2. Only the genuinely ambiguous tail — the 알려줘/알려주세요/확인해주세요 and
     "report back when X happens" crux that terminates *both* live questions and
     completions — falls through to a one-shot `claude -p` (haiku) classify.
  3. Everything else (plain declaratives, English completions) -> COMPLETE, instant.

stdin: the Stop hook payload JSON (carries transcript_path).
stdout: one line "<decision>\t<reason>\t<tail-excerpt>", decision in {question,complete}.
"""
import json
import os
import re
import subprocess
import sys
import time

MARKER = "묻고 싶은 것"
QMARK = re.compile(r"[?？]")

# Strong question / decision-request endings (Korean). Matched on the last two
# non-empty lines. Deliberately EXCLUDES the 알려/확인 report-back crux — those go
# to the LLM fallback, not here.
STRONG_Q = re.compile(
    r"나요|까요|ㄹ까|을까|할까|볼까|갈까|어떨까|어때|드릴까|넘길까|좋을까|맞을까"
    r"|진행할까|닫을까|남길까|보이나요|맞나요|시겠|원하시면|원하면"
    r"|동의(하나요|하시나요|해|하시면|하면)"
    r"|어느\s?(쪽|것|걸|게)"
    r"|선택(해|하)|골라|고르"
    r"|말씀|검토\s?부탁|승인(해|할)|정해\s?주|말해\s?줘|결정해"
)

# Genuinely ambiguous: the live-ask-vs-report-back crux. These tokens end *both*
# real questions ("어느 옵션인지 알려주세요") and completions ("결과 받으면 알려주세요"),
# so no regex can split them — route to the LLM.
AMBIG = re.compile(
    r"알려\s?(줘|주세요|주시면|주십시오|달라|주실|드릴)"
    r"|확인\s?(해\s?줘|해\s?주세요|해\s?주시면|부탁)"
    r"|(되면|받으면|끝나면|나오면|나면|완료되면|결과).{0,24}(주세요|주시면|알려)"
    r"|보내\s?(줘|주세요|주시면)"
)

LLM_MODEL = "claude-haiku-4-5"
LLM_TIMEOUT_S = 8

# Flush-lag poll (see resolve_tail). When the Stop hook fires, Claude Code may not
# have flushed THIS turn's assistant entries yet, so the transcript can still end at
# a prior entry — a stale tool_use (the turn continued from an AskUserQuestion) or a
# thinking chunk (end_turn, no text) written a beat before its text chunk. Either way
# the tail reads empty. The session is paused during the hook, so the only pending
# writes are this turn's own chunks: poll until the text lands. ~3s ceiling; a genuine
# interrupt (truly tool_use-final) pays the full budget then falls back to complete.
RETRIES = 20
RETRY_SLEEP_S = 0.15
# Kill switch for the LLM fallback (the A/B choice). "1" = fallback on (default,
# the user's pick); "0" = treat ambiguous tails as complete, fully offline.
LLM_FALLBACK = os.environ.get("SOUND_LLM_FALLBACK", "1") == "1"
# Diagnostics — the Stop-payload dump (.sound-last-payload.json) and the per-turn
# [diag] stderr line. Investigation scaffolding, default OFF; SOUND_DIAG=1 re-enables
# both. The dump writes the full payload (incl. last_assistant_message text) to disk
# every turn, so it must not ship default-on.
SOUND_DIAG = os.environ.get("SOUND_DIAG", "0") == "1"


def last_two_lines(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-2:])


def classify(tail):
    """Deterministic pass. Returns (decision, reason);
    decision in {"question", "complete", "ambiguous"}."""
    if not tail or not tail.strip():
        return "complete", "empty"
    if MARKER in tail:
        return "question", "marker"
    window = last_two_lines(tail)
    if QMARK.search(window):
        return "question", "qmark"
    if STRONG_Q.search(window):
        return "question", "polite-rx"
    if AMBIG.search(window):
        return "ambiguous", "ambig"
    return "complete", "default"


def _entry_text(msg):
    return "".join(
        b.get("text", "")
        for b in msg.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    )


def _read_entries(transcript_path):
    """(stop_reason, text, msg_id) per assistant entry in file order, or None if
    unreadable. errors='replace' so one stray non-UTF8 byte degrades to an empty
    tail (→ complete) instead of crashing the classifier mid-poll."""
    entries = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message")
                if isinstance(msg, dict):
                    entries.append(
                        (msg.get("stop_reason"), _entry_text(msg), msg.get("id")))
    except OSError:
        return None
    return entries


def _tail_from_entries(entries):
    """Text of the final turn IFF the LATEST assistant entry is end_turn.

    If it is a tool_use, the turn either ended mid-tool (interrupt) or is an
    AskUserQuestion wait — the latter is owned by the PermissionRequest sound, and
    an interrupt is not a question — so return "" (→ 'complete') rather than read a
    stale earlier turn. A visual turn can span several consecutive end_turn entries
    that share one message.id (e.g. a thinking chunk then a text chunk), so
    concatenate that trailing run — but ONLY within the final message.id. _read_entries
    drops user lines, so two DIFFERENT turns' end_turn entries sit adjacent in the
    list; stopping on stop_reason alone would merge them and leak the prior turn's
    text (it had non-empty text while this turn's text chunk was still mid-flush),
    classifying the WRONG turn. Gate on turn identity, not just stop_reason."""
    if not entries or entries[-1][0] != "end_turn":
        return ""
    final_id = entries[-1][2]
    texts = []
    for stop_reason, text, msg_id in reversed(entries):
        if stop_reason != "end_turn" or msg_id != final_id:
            break
        texts.append(text)
    return "".join(reversed(texts))


def extract_tail(transcript_path):
    entries = _read_entries(transcript_path)
    return _tail_from_entries(entries) if entries is not None else ""


def resolve_tail(read_entries, sleep, retries=RETRIES, delay=RETRY_SLEEP_S, diag=None):
    """Read the final-turn tail, polling past the transcript flush lag.

    A turn that ends essentially always has final prose, so an empty tail at Stop
    time means this turn's text chunk has not flushed yet — regardless of whether the
    file currently ends at a thinking chunk (end_turn, no text) OR at a stale prior
    tool_use (the common case when the turn continued from an AskUserQuestion: its
    end_turn chunks all land after the hook reads). The session is paused during the
    hook, so the only pending writes are THIS turn's chunks; poll on ANY empty tail
    until the text appears. Do NOT special-case tool_use-final — that guard could not
    tell a flush race from a real interrupt and dropped post-AskUserQuestion question
    turns to 'complete'. A genuine interrupt (truly tool_use-final, no text coming)
    simply exhausts the budget and falls back to complete. read_entries() and sleep()
    are injected so this is unit-testable without files or real waits."""
    entries = read_entries()
    init_last = entries[-1][0] if entries else None
    tail = _tail_from_entries(entries) if entries is not None else ""
    attempts = 0
    while (not tail.strip()) and attempts < retries:
        sleep(delay)
        entries = read_entries()
        tail = _tail_from_entries(entries) if entries is not None else ""
        attempts += 1
    if diag is not None:
        diag["init_last"] = init_last
        diag["attempts"] = attempts
        diag["final_last"] = entries[-1][0] if entries else None
        diag["tail_len"] = len(tail)
    return tail


def llm_classify(tail):
    """One-shot `claude -p` (haiku) classify of an ambiguous tail.

    `--setting-sources ""` loads ZERO settings sources, so the operator's ~/.claude
    hooks (Stop sound + status regenerator, narrative guard, tdd, devlog) do NOT fire
    in this child — the real isolation flag (`--settings '{}'` only MERGES settings,
    it does not suppress sources; mirrors merge_gate_local.py). The guard env vars are
    belt-and-suspenders so the Stop sound stays a no-op even if that ever regresses."""
    prompt = (
        "You are labeling the final message of a Claude Code session turn. Did the "
        "assistant ASK THE USER a question or request a decision/choice that needs an "
        "answer NOW, or did it REPORT task completion / ask to be told the result of a "
        "FUTURE external action? Reply with exactly one word: QUESTION or COMPLETE.\n\n"
        "--- final message ---\n" + tail[-1500:]
    )
    env = dict(os.environ)
    env["SOUND_CLASSIFY_RUNNING"] = "1"
    env["MERGE_GATE_PRODUCER_RUNNING"] = "1"
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", LLM_MODEL, "--setting-sources", "",
             "--strict-mcp-config", prompt],
            capture_output=True, text=True, timeout=LLM_TIMEOUT_S, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "complete", "llm-timeout"
    ans = (proc.stdout or "").strip().upper()
    if "QUESTION" in ans[:24]:
        return "question", "llm"
    return "complete", "llm-default"


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    # Diagnostics (opt-in via SOUND_DIAG): dump the raw Stop payload so we can see which
    # fields it carries and inspect any fallback flush-lag behaviour. Overwrites every
    # turn when on. Best-effort; never fatal.
    if SOUND_DIAG:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   ".sound-last-payload.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    diag = {}
    # PRIMARY (deterministic, race-free): the Stop payload carries the final assistant
    # message's text in last_assistant_message, built in-memory at stop time. Its CLI
    # schema literally states it "avoids the need to read and parse the transcript
    # file"; being in-memory it is immune to the transcript flush race that made a
    # just-asked question read as empty → complete, and it never spans turn boundaries.
    lam = payload.get("last_assistant_message")
    if isinstance(lam, str) and lam.strip():
        tail, diag["src"] = lam, "payload"
    else:
        # FALLBACK: last_assistant_message is optional (absent when the final message
        # has no text block — pure tool_use / thinking-only, which is never a pending
        # question) and may be missing on older CLIs. Poll the transcript past the
        # flush lag as a safety net.
        tp = payload.get("transcript_path") or ""
        if not tp or not os.path.isfile(tp):
            print("complete\tno-transcript\t")
            return
        tail = resolve_tail(lambda: _read_entries(tp), time.sleep, diag=diag)
        diag["src"] = "transcript"

    decision, reason = classify(tail)
    if decision == "ambiguous":
        if LLM_FALLBACK:
            decision, reason = llm_classify(tail)
        else:
            decision, reason = "complete", "ambig-default"
    if SOUND_DIAG:
        sys.stderr.write(
            f"[diag] src={diag.get('src')} init_last={diag.get('init_last')} "
            f"attempts={diag.get('attempts')} final_last={diag.get('final_last')} "
            f"tail_len={diag.get('tail_len', len(tail))}\n")
    excerpt = tail.replace("\n", " ").replace("\t", " ")[-60:]
    print(f"{decision}\t{reason}\t{excerpt}")


if __name__ == "__main__":
    main()
